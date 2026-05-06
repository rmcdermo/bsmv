#include "vdb_writer.hpp"

#include <openvdb/openvdb.h>

#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <limits>
#include <sstream>
#include <stdexcept>

namespace bsmv {
namespace {

inline std::size_t idx_fortran(int i, int j, int k, int nx, int ny, int /*nz*/) {
  return static_cast<std::size_t>(i) +
         static_cast<std::size_t>(nx) * (static_cast<std::size_t>(j) +
                                         static_cast<std::size_t>(ny) * static_cast<std::size_t>(k));
}

std::uint64_t add_grid_data_thresholded(
    openvdb::FloatGrid &grid,
    const std::vector<float> &arr,
    int nx, int ny, int nz,
    float cutoff) {
  auto accessor = grid.getAccessor();
  std::uint64_t active = 0;

  for (int k = 0; k < nz; ++k) {
    for (int j = 0; j < ny; ++j) {
      for (int i = 0; i < nx; ++i) {
        const float v = arr[idx_fortran(i, j, k, nx, ny, nz)];

        // OpenVDB is sparse, but only if we do not activate visually irrelevant
        // low-level background values. A cutoff of 0 restores the previous
        // behavior: any nonzero value becomes active.
        if (std::isfinite(v) && v > cutoff) {
          accessor.setValue(openvdb::Coord(i, j, k), v);
          ++active;
        }
      }
    }
  }

  // Collapse any uniform active regions into tiles when possible.
  grid.tree().prune();
  return active;
}

}  // namespace

std::tuple<double, double> minmax(const std::vector<float> &arr) {
  if (arr.empty()) return {0.0, 0.0};
  double mn = std::numeric_limits<double>::infinity();
  double mx = -std::numeric_limits<double>::infinity();
  for (const float v : arr) {
    const double d = static_cast<double>(v);
    if (d < mn) mn = d;
    if (d > mx) mx = d;
  }
  if (!std::isfinite(mn) || !std::isfinite(mx)) return {0.0, 0.0};
  return {mn, mx};
}

VdbWriteStats write_vdb(
    const std::filesystem::path &filepath,
    const std::vector<float> &temperature,
    const std::vector<float> &density,
    int nx, int ny, int nz,
    const VdbThresholdOptions &options) {

  auto temp_grid = openvdb::FloatGrid::create(/*background=*/0.0f);
  temp_grid->setName("temperature");
  temp_grid->setTransform(openvdb::math::Transform::createLinearTransform(1.0));

  auto dens_grid = openvdb::FloatGrid::create(/*background=*/0.0f);
  dens_grid->setName("density");
  dens_grid->setTransform(openvdb::math::Transform::createLinearTransform(1.0));

  VdbWriteStats stats;
  stats.temperature_active_voxels = add_grid_data_thresholded(
      *temp_grid, temperature, nx, ny, nz, options.temperature_cutoff);
  stats.density_active_voxels = add_grid_data_thresholded(
      *dens_grid, density, nx, ny, nz, options.density_cutoff);

  openvdb::GridPtrVec grids;
  if (stats.temperature_active_voxels > 0) {
    grids.push_back(temp_grid);
  }
  if (stats.density_active_voxels > 0) {
    grids.push_back(dens_grid);
  }

  // Important for thresholded sparse output: do not write empty VDB files.
  // Blender can import such files as VOLUME datablocks, but obj.data.grids
  // will be empty, which makes them impossible to shade and confuses the
  // sparse loader. The manifest should only reference files that actually
  // contain at least one named FloatGrid.
  if (grids.empty()) {
    std::error_code ec;
    std::filesystem::remove(filepath, ec);
    stats.wrote_file = false;
    return stats;
  }

  openvdb::io::File file(filepath.string());
  file.write(grids);
  file.close();

  stats.wrote_file = true;
  return stats;
}

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
    double temperature_cutoff,
    double density_cutoff) {
  const auto spacing_x = x.size() > 1 ? (x[1] - x[0]) : 1.0;
  const auto spacing_y = y.size() > 1 ? (y[1] - y[0]) : 1.0;
  const auto spacing_z = z.size() > 1 ? (z[1] - z[0]) : 1.0;

  std::ofstream out(filepath);
  if (!out) {
    throw std::runtime_error("Could not write manifest: " + filepath.string());
  }

  out << std::fixed << std::setprecision(9);
  out << "{\n";
  out << "  \"chid\": \"" << chid << "\",\n";
  out << "  \"mesh_id\": \"mesh_" << std::setw(4) << std::setfill('0') << mesh_index_1based << std::setfill(' ') << "\",\n";
  out << "  \"mesh_index_1based\": " << mesh_index_1based << ",\n";
  out << "  \"temperature_grid_name\": \"temperature\",\n";
  out << "  \"density_grid_name\": \"density\",\n";
  out << "  \"temperature_units\": \"degC_above_ambient\",\n";
  out << "  \"density_units\": \"kg_per_m3\",\n";
  out << "  \"ambient_c\": " << ambient_c << ",\n";
  out << "  \"temperature_decode_min_c\": " << temp_min_smv << ",\n";
  out << "  \"temperature_decode_max_c\": " << temp_max_smv << ",\n";
  out << "  \"smoke_mass_extinction\": " << smoke_mass_extinction << ",\n";
  out << "  \"temperature_activation_cutoff\": " << temperature_cutoff << ",\n";
  out << "  \"density_activation_cutoff\": " << density_cutoff << ",\n";
  out << "  \"node_centered\": true,\n";
  out << "  \"spacing\": [" << spacing_x << ", " << spacing_y << ", " << spacing_z << "],\n";
  out << "  \"origin\": [" << x.front() << ", " << y.front() << ", " << z.front() << "],\n";
  out << "  \"vdb_local_ijk0\": [0, 0, 0],\n";
  out << "  \"vdb_local_scale\": [1.0, 1.0, 1.0],\n";
  out << "  \"x_range\": [" << x.front() << ", " << x.back() << "],\n";
  out << "  \"y_range\": [" << y.front() << ", " << y.back() << "],\n";
  out << "  \"z_range\": [" << z.front() << ", " << z.back() << "],\n";
  out << "  \"shape\": [" << x.size() << ", " << y.size() << ", " << z.size() << "],\n";
  out << "  \"frames\": [\n";

  for (std::size_t i = 0; i < frames.size(); ++i) {
    const auto &fr = frames[i];
    out << "    {\n";
    out << "      \"frame_index\": " << fr.frame_index << ",\n";
    out << "      \"time\": " << fr.time << ",\n";
    out << "      \"filename\": \"" << fr.filename << "\",\n";
    out << "      \"temperature_min\": " << fr.temperature_min << ",\n";
    out << "      \"temperature_max\": " << fr.temperature_max << ",\n";
    out << "      \"density_min\": " << fr.density_min << ",\n";
    out << "      \"density_max\": " << fr.density_max << ",\n";
    out << "      \"temperature_active_voxels\": " << fr.temperature_active_voxels << ",\n";
    out << "      \"density_active_voxels\": " << fr.density_active_voxels << "\n";
    out << "    }" << (i + 1 < frames.size() ? "," : "") << "\n";
  }

  out << "  ]\n";
  out << "}\n";
}

}  // namespace bsmv
