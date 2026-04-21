#include "s3d_reader.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace bsmv {

std::int32_t S3dReader::read_record_len(std::ifstream &in) {
  std::int32_t n = 0;
  read_exact(in, reinterpret_cast<char *>(&n), sizeof(n));
  return n;
}

void S3dReader::read_exact(std::ifstream &in, char *dst, std::size_t n) {
  in.read(dst, static_cast<std::streamsize>(n));
  if (!in) {
    throw std::runtime_error("Unexpected EOF while reading S3D.");
  }
}

S3dReader::S3dReader(const std::filesystem::path &path) : path_(path), in_(path, std::ios::binary) {
  if (!in_) {
    throw std::runtime_error("Could not open S3D file: " + path.string());
  }

  const std::int32_t rec0 = read_record_len(in_);
  if (rec0 != 32) {
    throw std::runtime_error("Unexpected S3D header record length in " + path.string());
  }

  std::int32_t header_ints[8];
  read_exact(in_, reinterpret_cast<char *>(header_ints), sizeof(header_ints));
  const std::int32_t rec0_end = read_record_len(in_);
  if (rec0_end != rec0) {
    throw std::runtime_error("Mismatched S3D header record terminator in " + path.string());
  }

  header_.ibar = header_ints[3];
  header_.jbar = header_ints[5];
  header_.kbar = header_ints[7];
  header_.nx = header_.ibar + 1;
  header_.ny = header_.jbar + 1;
  header_.nz = header_.kbar + 1;
  header_.ncell = static_cast<std::size_t>(header_.nx) * static_cast<std::size_t>(header_.ny) *
                  static_cast<std::size_t>(header_.nz);

  first_frame_pos_ = in_.tellg();
}

void S3dReader::rewind() {
  in_.clear();
  in_.seekg(first_frame_pos_);
}

bool S3dReader::next_frame(S3dFrame &frame) {
  if (!in_.good()) return false;

  std::int32_t rec_len = 0;
  in_.read(reinterpret_cast<char *>(&rec_len), sizeof(rec_len));
  if (!in_) return false;  // EOF
  if (rec_len != 4) {
    throw std::runtime_error("Expected 4-byte time record in " + path_.string());
  }

  read_exact(in_, reinterpret_cast<char *>(&frame.time), sizeof(frame.time));
  const std::int32_t time_end = read_record_len(in_);
  if (time_end != 4) {
    throw std::runtime_error("Mismatched time record terminator in " + path_.string());
  }

  const std::int32_t sz_len = read_record_len(in_);
  if (sz_len != 8) {
    throw std::runtime_error("Expected 8-byte size record in " + path_.string());
  }

  read_exact(in_, reinterpret_cast<char *>(&frame.nchars_in), sizeof(frame.nchars_in));
  read_exact(in_, reinterpret_cast<char *>(&frame.nchars_out), sizeof(frame.nchars_out));
  const std::int32_t sz_end = read_record_len(in_);
  if (sz_end != 8) {
    throw std::runtime_error("Mismatched size record terminator in " + path_.string());
  }

  if (frame.nchars_in != static_cast<std::int32_t>(header_.ncell)) {
    throw std::runtime_error("Unexpected nchars_in in " + path_.string());
  }
  if (frame.nchars_out < 0) {
    throw std::runtime_error("Negative nchars_out in " + path_.string());
  }

  const std::int32_t payload_len = read_record_len(in_);
  if (payload_len != frame.nchars_out) {
    throw std::runtime_error("Payload record length does not match nchars_out in " + path_.string());
  }

  std::vector<std::uint8_t> compressed(static_cast<std::size_t>(frame.nchars_out));
  if (!compressed.empty()) {
    read_exact(in_, reinterpret_cast<char *>(compressed.data()), compressed.size());
  }
  const std::int32_t payload_end = read_record_len(in_);
  if (payload_end != payload_len) {
    throw std::runtime_error("Mismatched payload record terminator in " + path_.string());
  }

  frame.values.assign(header_.ncell, 0U);

  std::size_t in_pos = 0;
  std::size_t out_pos = 0;
  constexpr std::uint8_t mark = 255U;
  while (in_pos < compressed.size()) {
    std::uint8_t value = 0U;
    std::size_t repeats = 0;
    if (compressed[in_pos] == mark) {
      if (in_pos + 2 >= compressed.size()) {
        throw std::runtime_error("Malformed RLE payload in " + path_.string());
      }
      value = compressed[in_pos + 1];
      repeats = static_cast<std::size_t>(compressed[in_pos + 2]);
      in_pos += 3;
    } else {
      value = compressed[in_pos];
      repeats = 1;
      in_pos += 1;
    }

    if (out_pos + repeats > frame.values.size()) {
      throw std::runtime_error("Decoded payload would overflow output in " + path_.string());
    }
    std::fill(frame.values.begin() + static_cast<std::ptrdiff_t>(out_pos),
              frame.values.begin() + static_cast<std::ptrdiff_t>(out_pos + repeats), value);
    out_pos += repeats;
  }

  if (out_pos != frame.values.size()) {
    throw std::runtime_error("Decoded payload size mismatch in " + path_.string());
  }

  return true;
}

std::vector<S3dSizeFrame> read_s3d_size_file(const std::filesystem::path &path) {
  std::ifstream in(path);
  if (!in) {
    throw std::runtime_error("Could not open S3D size file: " + path.string());
  }

  std::vector<S3dSizeFrame> frames;
  std::string line;
  while (std::getline(in, line)) {
    if (line.empty()) continue;

    std::istringstream iss(line);
    S3dSizeFrame fr;
    if (!(iss >> fr.time >> fr.nchars_in >> fr.nchars_out >> fr.max_val)) {
      continue;
    }
    frames.push_back(fr);
  }
  return frames;
}

double median_spacing(const std::vector<double> &coords) {
  if (coords.size() < 2) return 1.0;
  std::vector<double> diffs;
  diffs.reserve(coords.size() - 1);
  for (std::size_t i = 0; i + 1 < coords.size(); ++i) {
    diffs.push_back(coords[i + 1] - coords[i]);
  }
  std::sort(diffs.begin(), diffs.end());
  const std::size_t mid = diffs.size() / 2;
  if (diffs.size() % 2 == 0) {
    return 0.5 * (diffs[mid - 1] + diffs[mid]);
  }
  return diffs[mid];
}

std::vector<float> decode_temperature_c(const std::vector<std::uint8_t> &raw, float tmin_c, float tmax_c) {
  std::vector<float> out(raw.size(), 0.0f);
  const float denom = 254.0f;
  const float span = tmax_c - tmin_c;
  for (std::size_t i = 0; i < raw.size(); ++i) {
    out[i] = (static_cast<float>(raw[i]) / denom) * span + tmin_c;
  }
  return out;
}

std::vector<float> decode_temperature_excess_c(
    const std::vector<std::uint8_t> &raw,
    float tmin_c,
    float tmax_c,
    float ambient_c) {
  auto t = decode_temperature_c(raw, tmin_c, tmax_c);
  for (auto &v : t) {
    v = std::max(v - ambient_c, 0.0f);
  }
  return t;
}

std::vector<float> decode_density_linear(const std::vector<std::uint8_t> &raw, float max_val) {
  std::vector<float> out(raw.size(), 0.0f);
  const float factor = max_val / 254.0f;
  for (std::size_t i = 0; i < raw.size(); ++i) {
    out[i] = static_cast<float>(raw[i]) * factor;
  }
  return out;
}

}  // namespace bsmv
