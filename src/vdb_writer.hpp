#pragma once

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
};

void write_vdb(
    const std::filesystem::path &filepath,
    const std::vector<float> &temperature,
    const std::vector<float> &density,
    int nx, int ny, int nz);

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
    double smoke_mass_extinction);

std::tuple<double, double> minmax(const std::vector<float> &arr);

}  // namespace bsmv
