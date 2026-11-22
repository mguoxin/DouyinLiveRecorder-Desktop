DouyinLiveRecorder-Desktop 内置 FFmpeg 说明

一、二进制来源与版本

- 版本：n9.0.2-61-gd59078794d-20261121
  （ffmpeg -version 首行为 "ffmpeg version n9.0.2-61-gd59078794d-20261121"）
- 构建：BtbN/FFmpeg-Builds，win64 LGPL 共享库构建（非 GPL）
- 发布页：https://github.com/BtbN/FFmpeg-Builds/releases/tag/latest
- 下载文件：ffmpeg-n9.0-latest-win64-lgpl-shared-9.0.zip
- 下载文件 SHA-256：
  653BD67689E5B31F2813F43E8CE5014F91B359D0E3A7780EFED3ED365A0F7FCC
- 下载时间：2026-11-22

本项目只随包分发以下文件（其余文件未分发）：

  ffmpeg.exe        13B9730A9A87EC17003DCAEF572A42C753346F22CE6007300B5494C97769BE93
  avcodec-63.dll    19A66CDE0201EDF3590CD37B0080ABB53AADE774BFF446D320F084E38DD8B22F
  avdevice-63.dll   C5D973545013FCFB9B98C3996E013C2FB0F590042D55BA3851D83DA710003041
  avfilter-12.dll   845B2AEC011E26A97920CC42BD6F5E490F345896A3590C91C6F1024E03001BB0
  avformat-63.dll   A800D467D85742E30F3A8B94CD90B960BF233E39DFEBCF3A347924A03FDDE1FD
  avutil-61.dll     99A5E20EF4F5079C082AEDAAF9A42192AFB8A83B3CD22EB60D653125577B3809
  swresample-7.dll  9A212A39850896EE0ACDAA6033201253D5E14154B5AB32771AB1C58954317A5D
  swscale-10.dll    297C8E602759F1E51CA109BCA0DDE7CB6FBDA5126AC32DA3024F924883BDAA47

以上文件与上游 BtbN 构建逐字节一致，本项目未修改。

二、许可证

- FFmpeg 主体：GNU Lesser General Public License v3.0 或更高版本（LGPL-3.0-or-later）。
  该构建未启用 --enable-gpl，未包含 libx264、libx265、libxvid 等 GPL 组件
  （配置中显式 --disable-libx264 --disable-libx265 --disable-libxvid）。
- 许可全文：本目录 LICENSE（LGPLv3 全文，与上游 COPYING.LGPLv3 一致，
  SHA-256 DA7EABB7BAFDF7D3AE5E9F223AA5BDC1EECE45AC569DC21B3B037520B4464768）。
- 归属声明与源码义务见下文第四节。

三、编译配置（ffmpeg -version 原样输出）

configuration: --prefix=/ffbuild/prefix --pkg-config-flags=--static --pkg-config=pkg-config
--cross-prefix=x86_64-w64-mingw32- --arch=x86_64 --target-os=mingw32 --enable-version3
--disable-debug --enable-shared --disable-static --disable-w32threads --enable-pthreads
--enable-iconv --enable-zlib --enable-libxml2 --enable-libvmaf --enable-fontconfig
--enable-libharfbuzz --enable-libfreetype --enable-libfribidi --enable-lcms2 --enable-vulkan
--enable-libdav1d --enable-libvorbis --enable-librav1e --enable-librsvg --disable-libxcb
--disable-xlib --disable-libpulse --enable-gmp --enable-lzma --enable-liblcevc-dec
--enable-opencl --enable-amf --enable-libaom --disable-avisynth --enable-chromaprint
--disable-libdavs2 --disable-libdvdread --disable-libdvdnav --disable-libfdk-aac
--enable-ffnvcodec --enable-cuda-llvm --disable-frei0r --enable-libgme --enable-libkvazaar
--enable-libaribb24 --enable-libaribcaption --enable-libass --enable-libbluray --enable-libjxl
--enable-libmp3lame --enable-libopus --enable-libplacebo --enable-librist --enable-libssh
--enable-libtheora --enable-libvpx --enable-libwebp --enable-libzmq --enable-lv2 --enable-libvpl
--enable-openal --enable-liboapv --enable-libopencore-amr --enable-libopencore-amrwb
--enable-libopenh264 --enable-libopenjpeg --enable-libopenmpt --disable-librubberband
--enable-schannel --enable-sdl2 --enable-libsnappy --enable-libsoxr --enable-libsrt
--enable-libsvtav1 --enable-libtwolame --enable-libuavs3d --disable-libdrm --enable-vaapi
--enable-vapoursynth --disable-libvidstab --enable-libvvenc --disable-whisper
--disable-libx264 --disable-libx265 --disable-libxavs2 --disable-libxvid --enable-libzimg
--disable-libzvbi --extra-cflags=-DLIBTWOLAME_STATIC --extra-cxxflags= --extra-libs=-lgomp
--extra-ldflags=-pthread --extra-ldexeflags= --cc=x86_64-w64-mingw32-gcc
--cxx=x86_64-w64-mingw32-g++ --ar=x86_64-w64-mingw32-gcc-ar
--ranlib=x86_64-w64-mingw32-gcc-ranlib --nm=x86_64-w64-mingw32-gcc-nm
--extra-version=20261121

录制实际使用的能力（由 build_record_command 生成）：
- 输入协议：http / https / tcp / tls（TLS 由 Windows schannel 提供）
- 解封装：flv / hls / mpegts 等
- 输出封装：mpegts，参数 -c copy（不转码）
未使用 x264 / x265 等编码器。

四、LGPL 合规说明

1. 调用方式：本程序通过独立子进程调用 ffmpeg.exe，不把 FFmpeg 链接进主程序。
2. 动态链接：avcodec / avformat / avutil / avfilter / avdevice / swresample / swscale
   均以独立 DLL 形式随包分发，位于本目录，用户可以直接用自行编译的同版本 DLL 替换，
   无需修改或重新链接本程序。
3. 未修改：本项目未修改 FFmpeg 及其 DLL 的源代码。
4. 对应源码：
   - FFmpeg 源码（版本对应 commit）：
     https://github.com/FFmpeg/FFmpeg/tree/d59078794d
   - FFmpeg 9.0.2 发布源码：https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz
   - BtbN 构建脚本与依赖版本锁定：https://github.com/BtbN/FFmpeg-Builds
5. 书面要约：如因网络原因无法获取上述源码，可向项目仓库提交 Issue 索取，
   本项目将提供对应版本的源码归档或下载指引。

五、DLL 内静态编译的第三方库

上述 avcodec/avformat/avfilter 等 DLL 内静态包含 FFmpeg 官方构建启用的第三方库，
主要为：iconv、zlib、libxml2、libvmaf、fontconfig、harfbuzz、freetype、fribidi、
lcms2、dav1d、libvorbis、rav1e、librsvg、gmp、xz/lzma、aom、chromaprint、libgme、
libkvazaar、aribb24、aribcaption、libass、libbluray、libjxl、libmp3lame、libopus、
libplacebo、librist、libssh、libtheora、libvpx、libwebp、zeromq、lv2、libvpl、openal、
oapv、opencore-amr、openh264、openjpeg、openmpt、SDL2、snappy、soxr、libsrt、svt-av1、
twolame、uavs3d、vapoursynth、vvenc、zimg 等。

这些库的许可证以 LGPL-2.1/LGPL-3.0、BSD、MIT、Apache-2.0、MPL-2.0、ISC、zlib 等为主，
版权归各自权利人。各库源码可从其官方仓库获取，具体版本由 BtbN 构建脚本锁定
（https://github.com/BtbN/FFmpeg-Builds/tree/master/scripts.d）。
本项目未修改这些库，也未将其链接进本项目主程序。

已知残余事项：部分 LGPL 库（如 fribidi、soxr、zimg、libmp3lame 等）静态编译在
FFmpeg 的 DLL 内。若要求严格满足 LGPL 关于“可重新链接”的条款，需要在收到请求时
提供这些库的可重链接目标文件，或改用自建最小依赖构建。当前以公开源码地址与构建
脚本作为源码提供方式，此项已在候选验收报告中登记为残余风险。

六、本说明不构成法律意见。

FFmpeg 是 FFmpeg 团队的商标。本项目与 FFmpeg 项目、BtbN 及 gyan.dev 无隶属关系。
