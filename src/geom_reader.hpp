#pragma once

#include "smv_parser.hpp"

#include <array>
#include <filesystem>
#include <string>
#include <vector>

namespace bsmv {

struct GeomTriMesh {
  int geom_index_1based = 0;
  std::vector<std::array<float, 3>> vertices;
  std::vector<std::array<int, 3>> faces_1based;
  std::vector<int> surf_ids;
  std::array<double, 6> bbox = {0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
};

struct GeomOutputInfo {
  int geom_index_1based = 0;
  std::string obj_filename;
  int n_vertices = 0;
  int n_faces = 0;
  std::array<double, 6> bbox = {0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
};

std::vector<GeomTriMesh> read_fds_ge_split_by_geom(
    const std::filesystem::path &ge_path,
    const std::filesystem::path &ge2_path,
    int n_geometry_hint,
    const GeomSmvBlock *smv_geom = nullptr);

void write_obj(
    const std::filesystem::path &path,
    const GeomTriMesh &mesh,
    const std::vector<SurfaceInfo> &surfaces,
    const GeomSmvEntry *geom_entry = nullptr);

void write_geometry_manifest(
    const std::filesystem::path &path,
    const std::string &chid,
    const std::filesystem::path &ge_path,
    const std::filesystem::path &ge2_path,
    const std::vector<GeomOutputInfo> &items);

void write_scene_manifest(
    const std::filesystem::path &path,
    const SmvData &smv);

}  // namespace bsmv
