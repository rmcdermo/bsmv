#pragma once

#include <array>
#include <filesystem>
#include <map>
#include <string>
#include <unordered_map>
#include <vector>

namespace bsmv {

struct SmokeFileEntry {
  int mesh_index_1based = 0;
  double value = 0.0;
  std::string filename;
  std::string quantity;
  std::string short_name;
  std::string units;
};

struct MeshGrid {
  int mesh_index_1based = 0;
  int ibar = 0;
  int jbar = 0;
  int kbar = 0;
  std::vector<double> x;
  std::vector<double> y;
  std::vector<double> z;
};

struct GeomSmvEntry {
  int geom_index_1based = 0;
  std::string metadata_line;
  int n_faces_hint = 0;
  std::array<double, 6> bbox = {0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
};

struct GeomSmvBlock {
  int n_geometry = 0;
  std::string ge_filename;
  std::vector<GeomSmvEntry> entries;
};

struct SmvData {
  std::string chid;
  double hrrpuv_min = 0.0;
  double hrrpuv_max = 1200.0;
  double temp_min = 20.0;
  double temp_max = 2000.0;
  std::map<int, MeshGrid> grids;
  std::vector<SmokeFileEntry> smoke_entries;
  GeomSmvBlock geom;
};

std::string trim(const std::string &s);
std::string upper_copy(const std::string &s);
std::string canonical_quantity(const std::string &name);
SmvData parse_smv_file(const std::filesystem::path &path);
std::unordered_map<int, SmokeFileEntry> find_smokf3d_entries(
    const SmvData &smv,
    const std::string &wanted_quantity);

}  // namespace bsmv
