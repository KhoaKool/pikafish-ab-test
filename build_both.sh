#!/usr/bin/env bash
# build_both.sh -- Build baseline (unpatched) + patched Pikafish into one
# output folder, ready to feed straight into pika_match.py.
#
# Chạy trong MSYS2 MINGW64 shell (không phải MSYS2 MSYS thường).
#
# USAGE:
#   ./build_both.sh <duong-dan-repo-pikafish-sach> <duong-dan-file.patch> [thu-muc-output]
#
# VÍ DỤ:
#   ./build_both.sh /c/Users/Khoa/Pikafish pikafish-aggro-FINAL3.patch ./match_bin
#
# Yêu cầu: <duong-dan-repo-pikafish-sach> phải là 1 git repo Pikafish CHƯA
# vá gì cả (script tự checkout đúng commit gốc 8ab92480, tự clone ra 2 bản
# riêng -- KHÔNG đụng vào repo gốc của bạn, an toàn để chạy nhiều lần).

set -euo pipefail

BASE_COMMIT="97133eebb6ed55e0bfa13262555c77d683d6ac0f"
ARCH="${ARCH:-x86-64-bmi2}"
COMP="${COMP:-mingw}"

if [ $# -lt 2 ]; then
    echo "Cách dùng: $0 <repo-pikafish-sach> <file.patch> [output-dir]"
    echo "Vi du:     $0 /c/Users/Khoa/Pikafish pikafish-aggro-FINAL3.patch ./match_bin"
    exit 1
fi

REPO=$(cd "$1" && pwd)
PATCH=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")
OUT="${3:-./match_bin}"
JOBS="$(nproc 2>/dev/null || echo 4)"

if [ ! -d "$REPO/.git" ]; then
    echo "LỖI: '$REPO' không phải git repo. Cần trỏ vào thư mục Pikafish gốc (có .git bên trong)."
    exit 1
fi
if [ ! -f "$PATCH" ]; then
    echo "LỖI: không tìm thấy file patch '$PATCH'"
    exit 1
fi

mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "== [1/6] Clone bản BASELINE (sạch, đúng commit $BASE_COMMIT) =="
git clone --quiet "$REPO" "$WORK/base"
git -C "$WORK/base" checkout --quiet "$BASE_COMMIT"

echo "== [2/6] Clone bản PATCHED =="
git clone --quiet "$REPO" "$WORK/patched"
git -C "$WORK/patched" checkout --quiet "$BASE_COMMIT"

echo "== [3/6] Kiểm tra + áp patch (dừng ngay nếu không vá được, KHÔNG đoán mò) =="
if ! git -C "$WORK/patched" apply --check "$PATCH"; then
    echo "LỖI: patch không áp được lên commit $BASE_COMMIT. Không build tiếp."
    echo "     -> Có thể repo bạn trỏ vào không đúng commit gốc, hoặc patch đã cũ."
    exit 1
fi
git -C "$WORK/patched" apply "$PATCH"

echo "== [4/6] Build BASELINE (ARCH=$ARCH COMP=$COMP, $JOBS luồng) =="
make -C "$WORK/base/src" -j"$JOBS" profile-build ARCH="$ARCH" COMP="$COMP"

echo "== [5/6] Build PATCHED (ARCH=$ARCH COMP=$COMP, $JOBS luồng) =="
make -C "$WORK/patched/src" -j"$JOBS" profile-build ARCH="$ARCH" COMP="$COMP"

echo "== [6/6] Copy binary ra $OUT =="
find_exe() {
    if [ -f "$1/pikafish.exe" ]; then echo "$1/pikafish.exe";
    elif [ -f "$1/pikafish" ]; then echo "$1/pikafish";
    else echo ""; fi
}
BASE_EXE=$(find_exe "$WORK/base/src")
PATCHED_EXE=$(find_exe "$WORK/patched/src")

if [ -z "$BASE_EXE" ] || [ -z "$PATCHED_EXE" ]; then
    echo "LỖI: build xong nhưng không thấy file thực thi. Xem log make ở trên."
    exit 1
fi

cp "$BASE_EXE" "$OUT/pikafish_base$([ "${BASE_EXE##*.}" = exe ] && echo .exe)"
cp "$PATCHED_EXE" "$OUT/pikafish_patched$([ "${PATCHED_EXE##*.}" = exe ] && echo .exe)"

echo ""
echo "== Smoke test (gửi 'uci' + 'isready', xem có 'uciok'/'readyok' không) =="
for exe in "$OUT"/pikafish_base* "$OUT"/pikafish_patched*; do
    [ -f "$exe" ] || continue
    echo "-- $exe --"
    printf 'uci\nisready\nquit\n' | "$exe" | grep -E "uciok|readyok" || echo "  !! KHÔNG thấy uciok/readyok -- binary có thể lỗi"
done

echo ""
echo "XONG. 2 file binary nằm ở: $OUT"
echo "Chạy tiếp:"
echo "  python pika_match.py --engine1 $OUT/pikafish_base.exe --engine2 $OUT/pikafish_patched.exe --games 200 --movetime 1000"
