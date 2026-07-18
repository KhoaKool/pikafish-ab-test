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
import re
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

    def new_game(self):
        self.send("ucinewgame")
        self.send("isready")
        self.wait_for("readyok")

    def go_and_get_move(self, moves, movetime_ms, timeout_s):
        pos_cmd = "position startpos"
        if moves:
            pos_cmd += " moves " + " ".join(moves)
        self.send(pos_cmd)
        self.send(f"go movetime {movetime_ms}")
        line = self.wait_for("bestmove", timeout=timeout_s)
        # "bestmove e2e4 ponder e7e5"  or  "bestmove (none)"
        parts = line.split()
        return parts[1] if len(parts) > 1 else "(none)"

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


def play_game(red_engine, black_engine, movetime_ms, max_plies, move_timeout_s):
    """red_engine moves first (Red always opens in Xiangqi). Returns
    (result, plies, reason) where result is 'red', 'black', or 'draw'."""
    red_engine.new_game()
    black_engine.new_game()

    moves = []
    for ply in range(max_plies):
        mover = red_engine if ply % 2 == 0 else black_engine
        move = mover.go_and_get_move(moves, movetime_ms, move_timeout_s)

        if move == "(none)":
            # Side to move has no legal move => that side loses (Xiangqi has
            # no stalemate-draw rule: no legal move is always a loss).
            loser_is_red = (ply % 2 == 0)
            return ("black" if loser_is_red else "red"), ply, "no_legal_move"

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

    try:
        for g in range(args.games):
            e2_is_red = (g % 2 == 0)  # alternate who opens each game
            red, black = (e2, e1) if e2_is_red else (e1, e2)

            for eng, name in ((red, "red"), (black, "black")):
                if eng.ensure_alive():
                    print(f"[game {g}] {name} engine ({eng.path}) had died -- restarted "
                          f"(see {eng.stderr_path} for why it crashed)")

            t0 = time.time()
            aborted_this_game = False
            try:
                result, plies, reason = play_game(red, black, args.movetime, args.max_plies, args.move_timeout)
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
                        "game": g, "e2_color": "red" if e2_is_red else "black",
                        "result": result, "e2_result": e2_result,
                        "plies": plies, "reason": reason, "seconds": round(dt, 1),
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
                "game": g, "e2_color": "red" if e2_is_red else "black",
                "result": result, "e2_result": e2_result,
                "plies": plies, "reason": reason, "seconds": round(dt, 1),
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
                                 ["game", "e2_color", "result", "e2_result", "plies", "reason", "seconds"])
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
