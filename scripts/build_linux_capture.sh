#!/bin/sh
# Run in a Debian build container; foreign development libraries are linked
# directly by the cross compiler. No Python is included or used by the capture.
set -eu
target_arch=$1
case "$target_arch" in
  x86) deb_arch=i386; triple=i686-linux-gnu; compiler=g++-i686-linux-gnu ;;
  x86_64) deb_arch=amd64; triple=x86_64-linux-gnu; compiler=g++ ;;
  armv7) deb_arch=armhf; triple=arm-linux-gnueabihf; compiler=g++-arm-linux-gnueabihf ;;
  aarch64) deb_arch=arm64; triple=aarch64-linux-gnu; compiler=g++-aarch64-linux-gnu ;;
  riscv64) deb_arch=riscv64; triple=riscv64-linux-gnu; compiler=g++-riscv64-linux-gnu ;;
  *) echo 'Unsupported architecture' >&2; exit 1 ;;
esac
dpkg --add-architecture "$deb_arch"
apt-get -o Acquire::Retries=3 update
apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
  cmake make pkg-config git "$compiler" "libatspi2.0-dev:$deb_arch" \
  "libx11-dev:$deb_arch" "libpng-dev:$deb_arch" "libjson-c-dev:$deb_arch" "zlib1g-dev:$deb_arch"
git config --global --add safe.directory /src
export PKG_CONFIG_LIBDIR="/usr/lib/$triple/pkgconfig:/usr/share/pkgconfig"
cmake -S capture/linux -B "build/linux-$target_arch" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_SYSTEM_NAME=Linux \
  -DCMAKE_CXX_COMPILER="$triple-g++" \
  -DCMAKE_EXE_LINKER_FLAGS='-static-libstdc++ -static-libgcc'
cmake --build "build/linux-$target_arch" -j2
mkdir -p dist
cp "build/linux-$target_arch/svgshot-capture-linux" "dist/svgshot-capture-linux-$target_arch"
if [ "$target_arch" = x86_64 ]; then
  test "$(dist/svgshot-capture-linux-x86_64 --version)" = "svgshot-capture-linux $(git rev-parse HEAD)"
  dist/svgshot-capture-linux-x86_64 --help
fi
