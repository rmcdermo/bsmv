#pragma once

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace bsmv {

struct S3dHeader {
  int32_t ibar = 0;
  int32_t jbar = 0;
  int32_t kbar = 0;
  int32_t nx = 0;
  int32_t ny = 0;
  int32_t nz = 0;
  std::size_t ncell = 0;
};

struct S3dFrame {
  float time = 0.0f;
  int32_t nchars_in = 0;
  int32_t nchars_out = 0;
  std::vector<std::uint8_t> values;  // size = nx*ny*nz, Fortran order
};

class S3dReader {
 public:
  explicit S3dReader(const std::filesystem::path &path);

  const std::filesystem::path &path() const { return path_; }
  const S3dHeader &header() const { return header_; }

  bool next_frame(S3dFrame &frame);
  void rewind();

 private:
  std::filesystem::path path_;
  std::ifstream in_;
  S3dHeader header_{};
  std::streampos first_frame_pos_{};

  static std::int32_t read_record_len(std::ifstream &in);
  static void read_exact(std::ifstream &in, char *dst, std::size_t n);
};

double median_spacing(const std::vector<double> &coords);
std::vector<float> decode_temperature_c(const std::vector<std::uint8_t> &raw);
std::vector<float> decode_temperature_excess_c(const std::vector<std::uint8_t> &raw, float ambient_c);
std::vector<float> decode_soot_density(const std::vector<std::uint8_t> &raw, double dx, double dy, double dz);

}  // namespace bsmv
