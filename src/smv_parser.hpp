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

struct SurfaceInfo {
  int surface_index = -1;  // FDS/Smokeview surface index, normally starts at 0.
  std::string id;
  double temperature = 5000.0;
  double emissivity = 1.0;
  int surf_type = 0;
  double texture_width = 0.0;
  double texture_height = 0.0;
  std::array<double, 3> rgb = {0.7, 0.7, 0.7};
  double transparency = 1.0;
  std::string texture_map;
};

struct VentOrigInfo {
  int vent_index_1based = 0;
  std::array<double, 6> bbox = {0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
  std::string id;
  std::string raw_line;
};

struct VentInfo {
  int mesh_index_1based = 0;
  int vent_index_1based = 0;
  bool circular = false;

  // Physical bbox in FDS coordinates: xmin,xmax,ymin,ymax,zmin,zmax.
  std::array<double, 6> bbox = {0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
  bool has_bbox = false;

  int ordinal = 0;
  int surf_index = -1;
  int color_index = 0;
  int type_index = 0;
  int ior = 0;

  // Optional explicit RGB written by FDS/SMV, values are 0..1.
  bool has_rgb = false;
  std::array<double, 3> rgb = {0.7, 0.7, 0.7};
  double transparency = 1.0;

  // Circular vent extras, if present.
  std::array<double, 3> center = {0.0, 0.0, 0.0};
  double radius = -1.0;

  std::string raw_geometry_line;
  std::string raw_display_line;
};

struct ObstInfo {
  int mesh_index_1based = 0;
  int obst_index_1based = 0;
  std::string raw_line;
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

  std::string default_surface_id;
  std::vector<SurfaceInfo> surfaces;
  std::vector<VentOrigInfo> vent_orig;
  std::vector<VentInfo> vents;
  std::vector<ObstInfo> obstacles;

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
