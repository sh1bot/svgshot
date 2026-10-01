#pragma once
#include <miniz.h>
#include <cstdint>
#include <iterator>

// seMA payload: "SVGSHOT\0", container version, JSON encoding, zlib compression,
// reserved zero, uncompressed uint32 length (big endian), zlib stream.
std::string embedded_png(std::string png, const std::string &snapshot)
{
    if (snapshot.size() > 64 * 1024 * 1024)
        throw std::runtime_error("UIA snapshot exceeds 64 MiB");
    mz_ulong compressed_size = mz_compressBound(static_cast<mz_ulong>(snapshot.size()));
    std::string compressed(compressed_size, '\0');
    if (mz_compress2(reinterpret_cast<unsigned char *>(compressed.data()), &compressed_size,
                     reinterpret_cast<const unsigned char *>(snapshot.data()),
                     static_cast<mz_ulong>(snapshot.size()), 9) != MZ_OK)
        throw std::runtime_error("Cannot compress UIA JSON");
    compressed.resize(compressed_size);
    auto big_endian = [](std::string &out, uint32_t value) {
        for (int shift = 24; shift >= 0; shift -= 8)
            out += static_cast<char>((value >> shift) & 255);
    };
    std::string payload("SVGSHOT\0\1\1\1\0", 12);
    big_endian(payload, static_cast<uint32_t>(snapshot.size()));
    payload += compressed;
    if (payload.size() > 16 * 1024 * 1024)
        throw std::runtime_error("Compressed UIA snapshot exceeds 16 MiB");
    const std::string signature("\x89PNG\r\n\x1a\n", 8);
    const std::string end("\0\0\0\0IEND\xae\x42\x60\x82", 12);
    if (png.size() < 20 || png.substr(0, 8) != signature || png.substr(png.size() - 12) != end)
        throw std::runtime_error("Cannot embed UIA in invalid PNG");
    std::string chunk;
    big_endian(chunk, static_cast<uint32_t>(payload.size()));
    chunk += "seMA";
    chunk += payload;
    big_endian(chunk, static_cast<uint32_t>(
                          mz_crc32(0, reinterpret_cast<const unsigned char *>(chunk.data() + 4),
                                   chunk.size() - 4)));
    png.insert(png.size() - 12, chunk);
    return png;
}
void write_capture(const std::filesystem::path &path, const std::string &png, bool replace = true)
{
    auto temporary = path;
    temporary += L"." + std::to_wstring(GetCurrentProcessId()) + L".tmp";
    {
        std::ofstream output(temporary, std::ios::binary | std::ios::trunc);
        output.write(png.data(), static_cast<std::streamsize>(png.size()));
        output.close();
        if (!output)
        {
            std::filesystem::remove(temporary);
            throw std::runtime_error("Cannot write embedded PNG");
        }
    }
    if (!MoveFileExW(temporary.c_str(), path.c_str(),
                     (replace ? MOVEFILE_REPLACE_EXISTING : 0) | MOVEFILE_WRITE_THROUGH))
    {
        auto error = GetLastError();
        std::filesystem::remove(temporary);
        throw std::runtime_error("Cannot replace capture with embedded PNG (Windows error " +
                                 std::to_string(error) + ")");
    }
}
