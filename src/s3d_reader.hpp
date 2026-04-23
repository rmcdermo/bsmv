#pragma once

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <vector>

namespace bsmv {

struct S3dHeader {
  int ibar = 0;
  int jbar = 0;
  int kbar = 0;
  int nx = 0;
  int ny = 0;
  int nz = 0;
  std::size_t ncell = 0;
};

struct S3dFrame {
  float time = 0.0f;
  std::int32_t nchars_in = 0;
  std::int32_t nchars_out = 0;
  std::vector<std::uint8_t> values;
};

struct S3dSizeFrame {
  double time = 0.0;
  std::int32_t nchars_in = 0;

  bool has_primary = false;
  std::int32_t nchars_out = 0;
  float max_val = 0.0f;

  bool has_secondary = false;
  std::int32_t nchars_out2 = 0;
  float max_val2 = 0.0f;
};

class S3dReader {
public:
  explicit S3dReader(const std::filesystem::path &path);

  void rewind();
  bool next_frame(S3dFrame &frame);

  const S3dHeader &header() const { return header_; }

private:
  static std::int32_t read_record_len(std::ifstream &in);
  static void read_exact(std::ifstream &in, char *dst, std::size_t n);

  std::filesystem::path path_;
  std::ifstream in_;
  std::streampos first_frame_pos_{};
  S3dHeader header_{};
};

std::vector<S3dSizeFrame> read_s3d_size_file(const std::filesystem::path &path);

double median_spacing(const std::vector<double> &coords);

std::vector<float> decode_temperature_c(
    const std::vector<std::uint8_t> &raw,
    float tmin_c,
    float tmax_c);

std::vector<float> decode_temperature_excess_c(
    const std::vector<std::uint8_t> &raw,
    float tmin_c,
    float tmax_c,
    float ambient_c);

std::vector<float> decode_density_linear(
    const std::vector<std::uint8_t> &raw,
    float max_val);

}  // namespace bsmv
