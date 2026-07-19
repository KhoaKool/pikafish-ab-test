#!/usr/bin/env python3
"""
pika_match.py -- A/B self-play harness for Pikafish (baseline vs patched).

WHY THIS EXISTS
----------------
Không có cách nào biết 1 patch làm engine mạnh hơn hay yếu hơn chỉ bằng cách
đọc code. Script này cho 2 binary (bản gốc và bản đã vá) tự đấu với nhau N
ván qua giao thức UCI, đảo màu đi trước mỗi ván để loại bỏ lợi thế tiên thủ,
và in ra tỉ số thắng/hòa/thua + phần trăm điểm + ước lượng chênh Elo.

THẬT THÀ VỀ GIỚI HẠN (đọc trước khi tin kết quả)
--------------------------------------------------
1. Phát hiện chiếu bí / hết nước đi: dùng tín hiệu "bestmove (none)" -- đây
   là tín hiệu CHẮC CHẮN đúng (Pikafish trả về khi hết nước đi hợp lệ).
2. Phát hiện HÒA (lặp thế 3 lần / 60 nước không ăn quân theo luật Xiangqi):
   script KHÔNG tự dựng bàn cờ để kiểm tra luật này chính xác 100%. Nó chỉ
   dùng 2 cách xấp xỉ:
     a) Giới hạn số nước tối đa (--max-plies), hết thì xử hòa.
     b) Phát hiện lặp lại chuỗi nước gần đây (heuristic, không phải luật
        chính thức) để dừng sớm các ván lặp vô nghĩa.
   => Điều này có nghĩa: kết quả "hòa" trong file output có thể không khớp
      100% với phán quyết thật của rule_judge() bên trong engine. Với vài
      trăm ván, sai số này thường nhỏ và không đổi kết luận tổng thể, nhưng
      đừng tin tuyệt đối 1-2 ván lẻ bị xử hòa do --max-plies.
3. Không kiểm tra hợp lệ nước đi (không tự implement luật Xiangqi) -- tin
   tưởng cả 2 engine trả về nước hợp lệ. Nếu 1 bên có bug sinh nước bất hợp
   lệ, script sẽ KHÔNG phát hiện được (đây là điều bạn phải tự kiểm tra qua
   file PGN/log nếu nghi ngờ).
4. Đây là công cụ ĐO XU HƯỚNG nhanh, không thay thế được SPRT thật (như
   fastchess/cutechess dùng cho Stockfish) nếu bạn cần độ tin cậy thống kê
   nghiêm ngặt. Coi kết quả là "có vẻ tốt hơn / có vẻ tệ hơn / chưa rõ", và
   chạy lại với --games lớn hơn nếu chênh lệch sát 50%.

CÁCH DÙNG
---------
    python pika_match.py --engine1 ./pikafish_base.exe --engine2 ./pikafish_patched.exe \
        --games 200 --movetime 1000 --max-plies 260 --out results.csv

    --engine1 / --engine2 : đường dẫn 2 file .exe (hoặc binary Linux)
    --movetime            : ms suy nghĩ mỗi nước (cố định, để công bằng)
    --max-plies           : số nửa-nước tối đa trước khi xử hòa (mặc định 260)
    --games                : số ván (sẽ tự đảo màu, nên dùng số chẵn)
    --e1-options / --e2-options : "Name1=Value1,Name2=Value2" gửi qua
                              setoption trước mỗi ván (vd Contempt=40)
    --out                  : file CSV ghi từng ván
"""

import argparse
import csv
import json
import math
import os
import queue
import random
import re
import urllib.parse
import urllib.request
import subprocess
import sys
import threading
import time


class Engine:
    """Thin UCI wrapper: non-blocking line reader via a background thread,
    so a hung/slow engine can never freeze the harness (subprocess pipes are
    notoriously not select()-able on Windows, hence the thread).

    IMPORTANT (fixed after a real bug found in production): stderr used to be
    silently discarded (subprocess.DEVNULL), which meant a genuine engine
    crash gave NO diagnostic information at all -- and every game after the
    crash silently failed with "Broken pipe" and got miscounted as a draw,
    corrupting the whole match's score. Both are fixed now: stderr goes to a
    real log file, and the engine process is restarted if it dies."""

    def __init__(self, path, options=None, tag="engine"):
        self.path = path
        self.options = options or {}
        safe_tag = re.sub(r"[^A-Za-z0-9_.-]", "_", tag)
        self.stderr_path = f"{safe_tag}_stderr.log"
        self._start_process()

    def _start_process(self):
        # Open in append mode so restarts don't erase earlier crash evidence.
        self._stderr_file = open(self.stderr_path, "a")
        # Run with cwd = the engine's own directory. Pikafish looks for its
        # default EvalFile (pikafish.nnue) relative to the binary's directory,
        # but running it from a DIFFERENT working directory (e.g. a harness
        # invoked from the repo root) can trip that lookup depending on how
        # the binary was built/packaged. Setting cwd here removes the
        # ambiguity entirely -- it's exactly what happens if a person cd's
        # into the folder and runs the exe by hand.
        engine_dir = os.path.dirname(os.path.abspath(self.path)) or "."
        engine_exe = os.path.abspath(self.path)
        self.proc = subprocess.Popen(
            [engine_exe],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
            text=True,
            bufsize=1,
            cwd=engine_dir,
        )
        self.q = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._handshake(self.options)

    def is_alive(self):
        return self.proc.poll() is None

    def ensure_alive(self):
        """Restart the engine process if it died since the last game. Returns
        True if a restart happened (so the caller can log it)."""
        if self.is_alive():
            return False
        try:
            self._stderr_file.close()
        except Exception:
            pass
        self._start_process()
        return True

    def _read_loop(self):
        for line in self.proc.stdout:
            self.q.put(line.rstrip("\n"))
        self.q.put(None)  # signal EOF

    def send(self, cmd):
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def readline(self, timeout=30.0):
        try:
            line = self.q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"{self.path}: no response within {timeout}s")
        if line is None:
            raise ConnectionError(f"{self.path}: engine process exited unexpectedly "
                                   f"(see {self.stderr_path} for its stderr output)")
        return line

    def wait_for(self, token, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.readline(timeout=max(0.1, deadline - time.time()))
            if token in line:
                return line
        raise TimeoutError(f"{self.path}: '{token}' not seen within {timeout}s")

    def _handshake(self, options):
        self.send("uci")
        self.wait_for("uciok")
        for name, value in options.items():
            self.send(f"setoption name {name} value {value}")
        self.send("isready")
        self.wait_for("readyok")

    def new_game(self, timeout_s=10.0):
        self.send("ucinewgame")
        self.send("isready")
        self.wait_for("readyok", timeout=timeout_s)

    def go_and_get_move(self, moves, movetime_ms, timeout_s):
        pos_cmd = "position startpos"
        if moves:
            pos_cmd += " moves " + " ".join(moves)
        self.send(pos_cmd)
        self.send(f"go movetime {movetime_ms}")
        last_depth = 0
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = self.readline(timeout=max(0.1, deadline - time.time()))
            if line.startswith("info") and " depth " in line:
                toks = line.split()
                try:
                    d = int(toks[toks.index("depth") + 1])
                    # Ignore depth from "currmove"-only lines etc; a normal
                    # PV info line's depth is monotonically non-decreasing,
                    # so just track the max seen.
                    last_depth = max(last_depth, d)
                except (ValueError, IndexError):
                    pass
            elif line.startswith("bestmove"):
                parts = line.split()
                move = parts[1] if len(parts) > 1 else "(none)"
                return move, last_depth
        raise TimeoutError(f"{self.path}: 'bestmove' not seen within {timeout_s}s")

    def set_multipv(self, k, timeout_s=10.0):
        self.send(f"setoption name MultiPV value {k}")
        self.send("isready")
        self.wait_for("readyok", timeout=timeout_s)

    def go_and_get_random_opening_move(self, moves, movetime_ms, timeout_s, k):
        """Picks a RANDOM move among the engine's own top-k MultiPV
        candidates instead of always the single best one. Every candidate
        comes straight from the engine's own legal-move generator (parsed
        from its real 'info ... multipv N ... pv <move> ...' output), so
        this can never produce an illegal move -- unlike a hand-written
        opening book, which risks silently breaking games if a move turns
        out wrong. Falls back to the plain bestmove if MultiPV parsing finds
        nothing (e.g. engine reports 0 lines before mating/being mated)."""
        pos_cmd = "position startpos"
        if moves:
            pos_cmd += " moves " + " ".join(moves)
        self.send(pos_cmd)
        self.send(f"go movetime {movetime_ms}")

        candidates = {}
        bestmove = "(none)"
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = self.readline(timeout=max(0.1, deadline - time.time()))
            if line.startswith("info") and " multipv " in line and " pv " in line:
                toks = line.split()
                try:
                    idx = int(toks[toks.index("multipv") + 1])
                    mv = toks[toks.index("pv") + 1]
                    candidates[idx] = mv
                except (ValueError, IndexError):
                    pass
            elif line.startswith("bestmove"):
                parts = line.split()
                bestmove = parts[1] if len(parts) > 1 else "(none)"
                break

        if not candidates:
            return bestmove
        return random.choice(list(candidates.values()))

    def get_fen(self, moves, timeout_s=10.0):
        """Uses Pikafish's 'd' debug command (prints 'Fen: <fen>' among other
        info) to get the FEN for the current position, without needing our
        own Xiangqi board implementation. 'd' has no completion marker of
        its own, so we follow it with 'isready' and collect everything up
        to 'readyok' -- readyok is guaranteed to come after all of 'd's
        output has been flushed, per the UCI protocol's synchronous
        command handling."""
        pos_cmd = "position startpos"
        if moves:
            pos_cmd += " moves " + " ".join(moves)
        self.send(pos_cmd)
        self.send("d")
        self.send("isready")
        fen = None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = self.readline(timeout=max(0.1, deadline - time.time()))
            if line.startswith("Fen:"):
                fen = line[len("Fen:"):].strip()
            elif "readyok" in line:
                break
        return fen

    def go_until_depth_or_timeout(self, fen, min_depth, max_time_s):
        """Analyzes a position given directly as a FEN (not a move list --
        used for standalone book-building on arbitrary positions). Sends a
        generous 'go movetime <max_time_s>' as an upper bound, but sends the
        real UCI 'stop' command the moment depth >= min_depth is seen,
        ending the search early instead of always waiting the full budget.
        This implements "depth phải đạt từ 30 trở lên HOẶC tối đa 5 phút cho
        thế cờ khó": whichever condition is met first ends the analysis,
        and whatever depth/move was reached by then is returned -- for a
        genuinely hard position that never reaches depth 30 even after the
        full 5 minutes, we still return its best result; deciding whether
        that's "good enough" is left to the caller.

        Returns (move, depth_reached, elapsed_seconds)."""
        self.send(f"position fen {fen}")
        self.send(f"go movetime {int(max_time_s * 1000)}")
        last_depth = 0
        move = "(none)"
        stopped_early = False
        t0 = time.time()
        deadline = t0 + max_time_s + 30  # hard safety margin beyond the engine's own movetime
        while time.time() < deadline:
            line = self.readline(timeout=max(0.1, deadline - time.time()))
            if line.startswith("info") and " depth " in line:
                toks = line.split()
                try:
                    d = int(toks[toks.index("depth") + 1])
                    last_depth = max(last_depth, d)
                    if last_depth >= min_depth and not stopped_early:
                        self.send("stop")
                        stopped_early = True
                except (ValueError, IndexError):
                    pass
            elif line.startswith("bestmove"):
                parts = line.split()
                move = parts[1] if len(parts) > 1 else "(none)"
                break
        return move, last_depth, time.time() - t0

    def quit(self):
        try:
            self.send("quit")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        try:
            self._stderr_file.close()
        except Exception:
            pass


def elo_from_score(score):
    if 0 < score < 1:
        return 400 * math.log10(score / (1 - score))
    return None


def fishtest_stats(wins, draws, losses):
    """Elo 95% confidence interval + LOS (likelihood of superiority), using
    the same formulas fishtest/cutechess-cli use: treat each game's result
    (1/0.5/0) as a sample, compute its variance empirically, then apply the
    normal approximation (valid once N is a few dozen+ games)."""
    n = wins + draws + losses
    if n == 0:
        return None
    p_w, p_d, p_l = wins / n, draws / n, losses / n
    score = p_w + 0.5 * p_d
    variance = p_w * (1 - score) ** 2 + p_d * (0.5 - score) ** 2 + p_l * (0 - score) ** 2
    stderr = math.sqrt(variance / n) if n > 0 else 0
    lo = elo_from_score(max(1e-9, min(1 - 1e-9, score - 1.96 * stderr)))
    hi = elo_from_score(max(1e-9, min(1 - 1e-9, score + 1.96 * stderr)))
    elo = elo_from_score(score)
    # LOS = P(true score > 0.5) via normal approximation around the
    # observed score -- standard definition used by fishtest.
    los = 0.5 * (1 + math.erf((score - 0.5) / (stderr * math.sqrt(2)))) if stderr > 0 else (1.0 if score > 0.5 else 0.0)
    return {"score": score, "elo": elo, "elo_lo": lo, "elo_hi": hi, "los": los, "n": n}


def write_github_summary(md):
    """Appends markdown to $GITHUB_STEP_SUMMARY so it renders directly on
    the workflow run page in GitHub -- no artifact download needed. No-op
    (safe) when not running inside GitHub Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a") as f:
        f.write(md + "\n")


def parse_options(spec):
    opts = {}
    if not spec:
        return opts
    for pair in spec.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            opts[k.strip()] = v.strip()
    return opts


def looks_repetitive(moves, window=8, repeats=3):
    """Approximate repetition heuristic (NOT the engine's real rule_judge):
    flags if the same block of `window` moves appears `repeats` times in a
    row at the tail of the move list. Used only as an early-exit for
    obviously stuck games; final ground truth for close results should
    still be spot-checked against real games, not this heuristic alone."""
    n = window * repeats
    if len(moves) < n:
        return False
    tail = moves[-n:]
    block = tail[:window]
    return all(tail[i * window:(i + 1) * window] == block for i in range(repeats))


CDB_URL = "http://www.chessdb.cn/chessdb.php"


def cdb_query_move(fen, timeout=5.0, prefer_random=True):
    """Queries chessdb.cn's Xiangqi Cloud Database for a book move at this
    position. Uses action=query (documented as returning a best/random/
    candidate move -- gives natural opening variety) rather than querybest
    (always the single best line), since we want diverse openings, not the
    same main line every time.

    NOTE (being upfront about a real limitation): this exact HTTP call could
    not be tested end-to-end from the sandbox this script was written in
    (no network route to chessdb.cn there). The request/response format
    below follows chessdb.cn's own published API docs
    (https://www.chessdb.cn/cloudbook_api_en.html) plus a working community
    script referencing the same endpoint, but you should sanity-check it
    once for real before trusting it in a long unattended run, e.g.:
        python3 -c "from pika_match import cdb_query_move as q; print(q('rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1'))"

    Returns a UCI move string, or None if CDB has no book data for this
    position (out of book / unknown / error) -- caller should fall back to
    its own move generation in that case.
    """
    action = "query" if prefer_random else "querybest"
    url = f"{CDB_URL}?action={action}&board={urllib.parse.quote(fen)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="ignore").strip()
    except Exception:
        return None  # network hiccup / CDB down -- just fall back, never crash the run

    # Documented replies: "move:XXXX", "egtb:XXXX", "search:XXXX" (candidate,
    # needs further engine processing -- we treat it as unusable here),
    # "unknown", "nobestmove", "invalid board".
    if text.startswith("move:") or text.startswith("egtb:"):
        return text.split(":", 1)[1].split(",")[0].strip()
    return None


def cdb_query_egtb_move(fen, timeout=5.0):
    """Queries CDB specifically for an EXACT endgame-tablebase (EGTB) move --
    NOT a regular cloud-analyzed move. This distinction matters: an "egtb:"
    reply is mathematically PROVEN correct (solved, not just deeply
    searched), so it's trustworthy regardless of what "depth" would even
    mean for it -- unlike a plain "move:" reply, which CDB's API does NOT
    report a depth for, so we have no honest basis to claim it meets a
    depth>=30 bar. Used by build_book.py as a free shortcut: skip the
    expensive local 5-minute analysis whenever CDB already has a proven
    endgame answer for this exact position.

    Returns a UCI move string, or None if CDB has no EGTB data here (very
    common outside actual reduced-material endgames -- that's expected, not
    an error, caller should fall back to local analysis)."""
    url = f"{CDB_URL}?action=querybest&board={urllib.parse.quote(fen)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="ignore").strip()
    except Exception:
        return None
    if text.startswith("egtb:"):
        return text.split(":", 1)[1].split(",")[0].strip()
    return None


def desktop_book_path(filename):
    home = os.environ.get("USERPROFILE") if os.name == "nt" else os.environ.get("HOME")
    return os.path.join(home or ".", "Desktop", filename)


def append_to_obk(path, fen, move, depth, source_tag):
    """Appends one qualifying (fen, move) pair to the .obk book file,
    deduped by FEN -- if this exact position is already in the book, skip
    it (matches the "don't log duplicates" rule used for the other logs in
    this project). Reads the whole file to check for dupes each time, which
    is fine for a book file meant to stay at most tens of thousands of
    lines; not meant for huge-scale book building."""
    fen_key = fen.split(" - ")[0]  # ignore halfmove/fullmove counters when deduping
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith(f"fen={fen_key}"):
                        return False  # already known, skip
    except Exception:
        pass
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"fen={fen} move={move} depth={depth} source={source_tag}\n")
        return True
    except Exception:
        return False


def generate_opening(engine, plies, multipv, movetime_ms, timeout_s, use_cdb=True):
    """Generates a single opening sequence (list of UCI moves), preferring
    REAL online book moves from chessdb.cn (the Xiangqi Cloud Database) when
    available, falling back to the engine's own random top-K MultiPV choice
    once CDB has no data for the position (i.e. once we're out of book).

    Returns (moves, cdb_plies) where cdb_plies is how many of the leading
    moves came from CDB -- the caller uses this to know where "book" ends
    and "the engine's own play" begins, which matters for deciding which
    positions are even eligible to be logged into the local .obk file (only
    positions reached AFTER leaving book should be logged -- see main())."""
    engine.new_game(timeout_s=timeout_s)
    if multipv > 1:
        engine.set_multipv(multipv, timeout_s=timeout_s)

    moves = []
    cdb_plies = 0
    still_in_cdb_book = use_cdb
    for ply in range(plies):
        move = None
        if still_in_cdb_book:
            fen = engine.get_fen(moves, timeout_s=timeout_s)
            move = cdb_query_move(fen, timeout=5.0) if fen else None
            if move is None:
                still_in_cdb_book = False  # out of CDB's book from here on
            else:
                cdb_plies = ply + 1

        if move is None:
            move = engine.go_and_get_random_opening_move(moves, movetime_ms, timeout_s, multipv)

        if move == "(none)":
            break  # got mated inside the "opening" -- just use what we have
        moves.append(move)

    if multipv > 1:
        engine.set_multipv(1, timeout_s=timeout_s)
    return moves, cdb_plies


def play_game(red_engine, black_engine, movetime_ms, max_plies, move_timeout_s,
               opening_moves=None, obk_path=None, obk_min_depth=30, candidates_path=None):
    """red_engine moves first (Red always opens in Xiangqi). Returns
    (result, plies, reason) where result is 'red', 'black', or 'draw'.

    opening_moves: an optional pre-generated move sequence (see
    generate_opening) that both engines are seeded with before real play
    starts. Passing the SAME sequence into two calls (with red/black
    swapped) is what "paired openings" means.

    obk_path: if set, every REAL move (i.e. after the opening phase -- book
    moves and random-diversity opening moves are never logged, only the
    engine's own independently-searched middlegame/endgame moves) that
    reached depth >= obk_min_depth at this game's movetime is appended to
    the .obk file directly. Moves that DIDN'T reach that depth (very likely
    at normal test movetimes -- depth 30 usually needs much longer thinking)
    are instead appended to candidates_path as {fen, move} pairs for a
    SEPARATE, slower deep-analysis pass (see build_book.py) that can spend
    up to several minutes per position -- doing that inline here would make
    every test match agonizingly slow."""
    red_engine.new_game(timeout_s=move_timeout_s)
    black_engine.new_game(timeout_s=move_timeout_s)

    moves = list(opening_moves) if opening_moves else []
    start_ply = len(moves)

    if looks_repetitive(moves):
        return "draw", start_ply, "repetition_heuristic"

    for ply in range(start_ply, max_plies):
        mover = red_engine if ply % 2 == 0 else black_engine

        fen_before = None
        if obk_path and mover.is_alive():
            fen_before = mover.get_fen(moves, timeout_s=move_timeout_s)

        move, depth = mover.go_and_get_move(moves, movetime_ms, move_timeout_s)

        if move == "(none)":
            # Side to move has no legal move => that side loses (Xiangqi has
            # no stalemate-draw rule: no legal move is always a loss).
            loser_is_red = (ply % 2 == 0)
            return ("black" if loser_is_red else "red"), ply, "no_legal_move"

        if obk_path and fen_before:
            if depth >= obk_min_depth:
                append_to_obk(obk_path, fen_before, move, depth, os.path.basename(mover.path))
            elif candidates_path:
                append_to_obk(candidates_path, fen_before, move, depth, os.path.basename(mover.path))

        moves.append(move)

        if looks_repetitive(moves):
            return "draw", ply + 1, "repetition_heuristic"

    return "draw", max_plies, "max_plies_reached"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine1", required=True, help="Path to baseline engine binary")
    ap.add_argument("--engine2", required=True, help="Path to patched engine binary")
    ap.add_argument("--games", type=int, default=100)
    ap.add_argument("--movetime", type=int, default=1000, help="ms per move")
    ap.add_argument("--max-plies", type=int, default=260)
    ap.add_argument("--opening-plies", type=int, default=0,
                     help="First N plies pick randomly among the mover's own top-K MultiPV "
                          "candidates instead of always its best move, to create game-to-game "
                          "variety. 0 = disabled (every game starts identically -- near-identical "
                          "engines will then draw constantly and tell you nothing).")
    ap.add_argument("--opening-multipv", type=int, default=4,
                     help="K in the above -- how many top candidates to randomly choose among.")
    ap.add_argument("--opening-movetime", type=int, default=100,
                     help="ms per move during the opening phase (kept short -- these moves don't "
                          "need to be deep, just legal and reasonable).")
    ap.add_argument("--use-cdb", action="store_true",
                     help="Prefer real book moves from chessdb.cn (Xiangqi Cloud Database) during "
                          "the opening phase over the engine's own random MultiPV pick, falling "
                          "back automatically once CDB has no data for a position.")
    ap.add_argument("--obk-path", default="",
                     help="If set, real (post-opening) moves that reach --obk-min-depth are logged "
                          "here in .obk format -- a growing book of high-confidence middlegame/"
                          "endgame moves from actual self-play, deduped by position.")
    ap.add_argument("--obk-min-depth", type=int, default=30,
                     help="Minimum search depth for a move to qualify for direct .obk logging.")
    ap.add_argument("--obk-candidates-path", default="",
                     help="Where to queue positions whose move didn't reach --obk-min-depth at "
                          "test movetime -- feed this file to build_book.py for a separate, slower "
                          "deep-analysis pass (up to several minutes per position) that promotes "
                          "qualifying ones into --obk-path.")
    ap.add_argument("--move-timeout", type=float, default=30.0,
                     help="Seconds to wait for a single bestmove before aborting the game")
    ap.add_argument("--e1-options", default="", help="e.g. Contempt=0,MaxThinkTime=30000")
    ap.add_argument("--e2-options", default="", help="e.g. Contempt=40,MaxThinkTime=30000")
    ap.add_argument("--out", default="pika_match_results.csv")
    ap.add_argument("--summary-json", default="",
                     help="Optional path to write a machine-readable {wins,draws,losses,score,elo_diff} summary, for CI")
    ap.add_argument("--min-score", type=float, default=None,
                     help="If set, exit with code 1 when engine2's final score is below this "
                          "(0.0-1.0) -- lets a CI job fail visibly when the patch looks worse")
    args = ap.parse_args()

    e1_opts = parse_options(args.e1_options)
    e2_opts = parse_options(args.e2_options)

    print(f"Starting engine1 ({args.engine1}) ...")
    e1 = Engine(args.engine1, e1_opts, tag="engine1")
    print(f"Starting engine2 ({args.engine2}) ...")
    e2 = Engine(args.engine2, e2_opts, tag="engine2")

    # score is always tracked from engine2's ("patched") point of view.
    # aborted (crash/timeout) games are NOT draws -- a game we never actually
    # finished tells us nothing about relative strength, and silently
    # counting it as a draw was a real bug: it drags every score toward 50%
    # regardless of true strength difference. Aborted games are excluded from
    # score and reported separately instead.
    e2_wins = e2_losses = draws = aborted = 0
    consecutive_aborts = 0
    rows = []
    current_opening = []

    try:
        for g in range(args.games):
            e2_is_red = (g % 2 == 0)  # alternate who opens each game
            red, black = (e2, e1) if e2_is_red else (e1, e2)

            for eng, name in ((red, "red"), (black, "black")):
                if eng.ensure_alive():
                    print(f"[game {g}] {name} engine ({eng.path}) had died -- restarted "
                          f"(see {eng.stderr_path} for why it crashed)")

            # Paired openings (fishtest-style): every EVEN game generates a
            # fresh random opening; the following ODD game replays the exact
            # same opening with colors swapped. This cancels out most of the
            # "this particular opening just happens to favor one side"
            # noise, so far fewer total games are needed to see a real
            # strength difference than with an independent random opening
            # every single game.
            if args.opening_plies > 0:
                if g % 2 == 0:
                    try:
                        current_opening, cdb_plies = generate_opening(
                            e1, args.opening_plies, args.opening_multipv,
                            args.opening_movetime, args.move_timeout, use_cdb=args.use_cdb)
                        if cdb_plies:
                            print(f"[game {g}] opening: {cdb_plies}/{len(current_opening)} "
                                  f"plies from chessdb.cn book")
                    except (TimeoutError, ConnectionError) as exc:
                        print(f"[game {g}] opening generation failed ({exc}), using empty opening")
                        current_opening = []
            else:
                current_opening = []

            t0 = time.time()
            aborted_this_game = False
            try:
                result, plies, reason = play_game(
                    red, black, args.movetime, args.max_plies, args.move_timeout,
                    opening_moves=current_opening,
                    obk_path=(args.obk_path or None), obk_min_depth=args.obk_min_depth,
                    candidates_path=(args.obk_candidates_path or None))
            except (TimeoutError, ConnectionError) as exc:
                print(f"[game {g}] ABORTED: {exc}")
                result, plies, reason = "aborted", 0, f"aborted:{exc}"
                aborted_this_game = True
            dt = time.time() - t0

            if aborted_this_game:
                aborted += 1
                consecutive_aborts += 1
                e2_result = "aborted"
                if consecutive_aborts >= 3:
                    print(f"\n[DỪNG SỚM] {consecutive_aborts} ván liên tiếp bị abort -- "
                          f"engine đang crash lặp lại, không phải sự cố ngẫu nhiên. "
                          f"Kiểm tra engine1_stderr.log / engine2_stderr.log để biết lý do thật.")
                    rows.append({
                        "game": g, "pair_id": g // 2, "e2_color": "red" if e2_is_red else "black",
                        "result": result, "e2_result": e2_result,
                        "plies": plies, "reason": reason, "seconds": round(dt, 1),
                        "opening": " ".join(current_opening),
                    })
                    break
            else:
                consecutive_aborts = 0
                if result == "draw":
                    draws += 1
                    e2_result = "draw"
                else:
                    e2_won = (result == "red" and e2_is_red) or (result == "black" and not e2_is_red)
                    if e2_won:
                        e2_wins += 1
                        e2_result = "win"
                    else:
                        e2_losses += 1
                        e2_result = "loss"

            rows.append({
                "game": g, "pair_id": g // 2, "e2_color": "red" if e2_is_red else "black",
                "result": result, "e2_result": e2_result,
                "plies": plies, "reason": reason, "seconds": round(dt, 1),
                "opening": " ".join(current_opening),
            })

            played = e2_wins + draws + e2_losses
            score = (e2_wins + 0.5 * draws) / played if played else float("nan")
            print(f"[{g + 1}/{args.games}] e2={e2_result:8s} ({reason}, {plies}p, {dt:.0f}s) "
                  f"| hoàn tất: W{e2_wins} D{draws} L{e2_losses}  aborted={aborted}  score={score:.3f}")
    finally:
        e1.quit()
        e2.quit()

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                                 ["game", "pair_id", "e2_color", "result", "e2_result", "plies",
                                  "reason", "seconds", "opening"])
        writer.writeheader()
        writer.writerows(rows)

    played = e2_wins + draws + e2_losses
    if played == 0:
        print(f"Không có ván nào hoàn thành ({aborted} ván bị abort). "
              f"Xem engine1_stderr.log / engine2_stderr.log để biết engine crash vì sao.")
        write_github_summary(
            f"## ❌ Pikafish A/B Test: KHÔNG có ván nào hoàn thành\n\n"
            f"**{aborted} ván bị abort** (engine crash/treo) trước khi có bất kỳ ván nào chơi xong.\n\n"
            f"Xem artifact `engine1_stderr.log` / `engine2_stderr.log`, hoặc bước "
            f"\"Smoke test\" phía trên để biết engine nào crash và vì sao."
        )
        if args.summary_json:
            with open(args.summary_json, "w") as jf:
                json.dump({"played": 0, "wins": 0, "draws": 0, "losses": 0, "aborted": aborted,
                           "score": None, "elo_diff": None}, jf, indent=2)
        sys.exit(1)

    stats = fishtest_stats(e2_wins, draws, e2_losses)
    score = stats["score"]

    print("\n=== KẾT QUẢ CUỐI (engine2 = bản patched), kiểu fishtest ===")
    print(f"Total/Win/Draw/Lose : {played} / {e2_wins} / {draws} / {e2_losses}")
    print(f"WinRate     : {score:.2%}")
    if stats["elo"] is not None:
        print(f"Elo         : {stats['elo']:+.2f} [{stats['elo_lo']:+.2f}, {stats['elo_hi']:+.2f}]  (95% CI)")
    print(f"LOS         : {stats['los']:.2%}  (xác suất engine2 thực sự mạnh hơn engine1)")
    if aborted > 0:
        print(f"\n!! CẢNH BÁO: {aborted} ván bị abort, KHÔNG tính vào các số trên.")

    summary_md = [
        f"## {'✅' if score >= 0.5 else '⚠️'} Pikafish A/B Test Results (fishtest-style)",
        "",
        f"| | |",
        f"|---|---|",
        f"| Total/Win/Draw/Lose | {played} / {e2_wins} / {draws} / {e2_losses} |",
        f"| WinRate | {score:.2%} |",
    ]
    if stats["elo"] is not None:
        summary_md.append(f"| Elo | {stats['elo']:+.2f} [{stats['elo_lo']:+.2f}, {stats['elo_hi']:+.2f}] (95% CI) |")
    summary_md.append(f"| LOS | {stats['los']:.2%} |")
    if aborted > 0:
        summary_md.append(f"| ⚠️ Aborted (crash/timeout, excluded) | {aborted} |")
    summary_md.append("")
    summary_md.append(
        "> Score < 50% hoặc khoảng tin cậy Elo bao gồm số âm lớn ⇒ patch có thể đang làm "
        "engine YẾU ĐI, không nên kết luận vội với mẫu nhỏ."
    )
    write_github_summary("\n".join(summary_md))

    print(f"\nChi tiết từng ván: {args.out}")
    print("\nLƯU Ý: đọc phần THẬT THÀ VỀ GIỚI HẠN ở đầu file script trước khi kết luận.")

    if args.summary_json:
        with open(args.summary_json, "w") as jf:
            json.dump({"played": played, "wins": e2_wins, "draws": draws, "losses": e2_losses,
                       "aborted": aborted, "score": score, "elo": stats["elo"],
                       "elo_ci": [stats["elo_lo"], stats["elo_hi"]], "los": stats["los"]}, jf, indent=2)

    if args.min_score is not None and score < args.min_score:
        print(f"\n[CI GATE] score {score:.3f} < --min-score {args.min_score:.3f} -> exit 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
