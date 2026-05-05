#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <tuple>
#include <vector>

namespace bsmv {

struct ManifestFrameInfo {
  int frame_index = 0;
  double time = 0.0;
  std::string filename;
  double temperature_min = 0.0;
  double temperature_max = 0.0;
  double density_min = 0.0;
  double density_max = 0.0;
  std::uint64_t temperature_active_voxels = 0;
  std::uint64_t density_active_voxels = 0;
};

struct VdbThresholdOptions {
  float temperature_cutoff = 20.0f; // degC above ambient
  float density_cutoff = 1.0e-8f;   // kg/m3
};

struct VdbWriteStats {
  std::uint64_t temperature_active_voxels = 0;
  std::uint64_t density_active_voxels = 0;
  bool wrote_file = false;
};

VdbWriteStats write_vdb(
    const std::filesystem::path &filepath,
    const std::vector<float> &temperature,
    const std::vector<float> &density,
    int nx, int ny, int nz,
    const VdbThresholdOptions &options = {});

void write_manifest(
    const std::filesystem::path &filepath,
    const std::string &chid,
    int mesh_index_1based,
    const std::vector<double> &x,
    const std::vector<double> &y,
    const std::vector<double> &z,
    const std::vector<ManifestFrameInfo> &frames,
    double ambient_c,
    double temp_min_smv,
    double temp_max_smv,
    double smoke_mass_extinction,
    double temperature_cutoff = 20.0,
    double density_cutoff = 1.0e-8);

std::tuple<double, double> minmax(const std::vector<float> &arr);

}  // namespace bsmv
