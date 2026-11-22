#!/bin/sh
set -eu

source_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
data_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}
prefix=$data_dir/bencher-linux/venv
bin_dir=$HOME/.local/bin
desktop_dir=$data_dir/applications
mkdir -p "$bin_dir" "$desktop_dir"
for executable in bencher bencher-linux; do
    link=$bin_dir/$executable
    if { [ -e "$link" ] || [ -L "$link" ]; } && [ "$(readlink "$link" || true)" != "$prefix/bin/$executable" ]; then
        printf 'Existing executable at %s. Move it before installing.\n' "$link" >&2
        exit 1
    fi
done
python3 -m venv --system-site-packages "$prefix"
"$prefix/bin/python" -m pip install --upgrade "$source_dir"
ln -sfn "$prefix/bin/bencher" "$bin_dir/bencher"
ln -sfn "$prefix/bin/bencher-linux" "$bin_dir/bencher-linux"
if command -v cc >/dev/null && command -v c++ >/dev/null; then
    "$prefix/bin/bencher" build-probe || printf 'Native probe build incomplete. Run bencher doctor for available sources.\n'
fi
cat > "$desktop_dir/io.github.zotvn.BencherLinux.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Bencher Linux
Comment=Record and inspect gaming benchmarks
Exec="$prefix/bin/bencher" gui
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
DESKTOP
"$prefix/bin/bencher" doctor || true
printf 'Open Bencher Linux from your app launcher, or run %s gui.\n' "$bin_dir/bencher"
