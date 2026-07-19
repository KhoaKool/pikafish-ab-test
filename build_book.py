#!/usr/bin/env python3
"""
build_book.py -- promotes candidate positions into a high-quality .obk book.

WHY THIS EXISTS
----------------
During normal A/B testing (pika_match.py --obk-candidates-path ...), every
real middlegame/endgame move that DIDN'T reach depth 30 at the test's fast
movetime gets queued here instead of being discarded. Re-analyzing every
such position for up to 5 minutes INSIDE the test match would make every
match agonizingly slow -- so it's a separate, slower pass you run
afterwards (or on a schedule / a second machine) whenever you want to grow
the book.

CONDITION (as specified): a position is promoted once its analysis reaches
depth >= --min-depth (default 30), OR once --max-time seconds (default 300
= 5 minutes) have been spent on it, whichever comes first. Uses the real
UCI 'stop' command to end the search the instant the depth condition is
met, rather than always waiting out the full time budget.

USAGE
-----
    python3 build_book.py --engine ./pikafish.exe \\
        --candidates pikafish_book_candidates.obk \\
        --obk pikafish_book.obk \\
        --min-depth 30 --max-time 300

Processed candidates are removed from the candidates file as they're
promoted, so re-running this script only works on what's left -- safe to
interrupt and resume.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pika_match import Engine, append_to_obk, cdb_query_egtb_move  # noqa: E402


def parse_candidate_line(line):
    """Parses a 'fen=... move=... depth=... source=...' line. The FEN
    itself may contain spaces, so we can't just split on whitespace --
    extract by locating each 'key=' marker in order."""
    keys = ["fen", "move", "depth", "source"]
    positions = []
    for k in keys:
        idx = line.find(f"{k}=")
        if idx == -1:
            return None
        positions.append((k, idx))
    positions.sort(key=lambda kv: kv[1])
    result = {}
    for i, (k, idx) in enumerate(positions):
        start = idx + len(k) + 1
        end = positions[i + 1][1] if i + 1 < len(positions) else len(line)
        result[k] = line[start:end].strip()
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", required=True, help="UCI engine binary to analyze with")
    ap.add_argument("--engine-options", default="", help="e.g. Threads=4,Hash=1024")
    ap.add_argument("--candidates", required=True, help="Candidates file from pika_match.py --obk-candidates-path")
    ap.add_argument("--obk", required=True, help="Output .obk book file to promote qualifying positions into")
    ap.add_argument("--min-depth", type=int, default=30)
    ap.add_argument("--max-time", type=float, default=300.0, help="Seconds (default 300 = 5 minutes)")
    ap.add_argument("--limit", type=int, default=0, help="Stop after processing this many positions (0 = no limit)")
    ap.add_argument("--use-cdb", action="store_true",
                     help="Before spending up to --max-time analyzing a position locally, ask "
                          "chessdb.cn for a proven EGTB (endgame tablebase) move first -- free, "
                          "near-instant, and mathematically exact when available (common for "
                          "reduced-material endgames). Only EGTB replies are trusted this way; "
                          "CDB's regular cloud moves don't report a depth, so we can't honestly "
                          "claim they meet --min-depth and fall back to local analysis for those.")
    args = ap.parse_args()

    if not os.path.exists(args.candidates):
        print(f"Không tìm thấy file candidates: {args.candidates}")
        sys.exit(1)

    with open(args.candidates, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f if ln.strip()]

    if not lines:
        print("Candidates file rỗng -- không có gì để phân tích.")
        return

    opts = {}
    for pair in args.engine_options.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            opts[k.strip()] = v.strip()

    print(f"Bắt đầu engine ({args.engine}) ...")
    engine = Engine(args.engine, opts, tag="build_book")

    processed = 0
    promoted = 0
    remaining = list(lines)

    try:
        for i, line in enumerate(lines):
            if args.limit and processed >= args.limit:
                break

            entry = parse_candidate_line(line)
            if not entry:
                print(f"[{i}] bỏ qua dòng không đọc được: {line[:60]}...")
                remaining.remove(line)
                continue

            fen = entry["fen"]
            t0 = time.time()

            if args.use_cdb:
                egtb_move = cdb_query_egtb_move(fen, timeout=5.0)
                if egtb_move:
                    added = append_to_obk(args.obk, fen, egtb_move, 999, "cdb_egtb")
                    print(f"[{i+1}/{len(lines)}] CDB đã có sẵn kết quả EGTB (đã giải chính xác, "
                          f"không cần phân tích) -> move={egtb_move}"
                          + ("  ✅ đã thêm vào " + args.obk if added else "  (đã có sẵn, bỏ qua)"))
                    remaining.remove(line)
                    processed += 1
                    if added:
                        promoted += 1
                    continue

            print(f"[{i+1}/{len(lines)}] Đang phân tích (tối đa {args.max_time:.0f}s, "
                  f"dừng sớm nếu đạt depth {args.min_depth}) ...")

            if not engine.is_alive():
                print("  engine đã chết, khởi động lại...")
                engine.ensure_alive()

            try:
                move, depth, elapsed = engine.go_until_depth_or_timeout(
                    fen, args.min_depth, args.max_time)
            except (TimeoutError, ConnectionError) as exc:
                print(f"  LỖI: {exc} -- bỏ qua vị trí này, giữ lại trong candidates để thử lại sau")
                processed += 1
                continue

            print(f"  -> move={move} depth={depth} ({elapsed:.0f}s)")

            if move != "(none)":
                added = append_to_obk(args.obk, fen, move, depth, "build_book")
                if added:
                    promoted += 1
                    print(f"  ✅ đã thêm vào {args.obk}")
                else:
                    print(f"  (đã có sẵn trong {args.obk}, bỏ qua trùng lặp)")

            remaining.remove(line)
            processed += 1

            # Save progress after every position, not just at the end --
            # if this script gets interrupted, already-processed positions
            # are not re-analyzed on the next run.
            with open(args.candidates, "w", encoding="utf-8") as f:
                f.write("\n".join(remaining) + ("\n" if remaining else ""))
    finally:
        engine.quit()

    print(f"\nXong. Đã xử lý {processed} vị trí, thêm mới {promoted} vào {args.obk}.")
    print(f"Còn lại {len(remaining)} vị trí chưa xử lý trong {args.candidates}.")


if __name__ == "__main__":
    main()
