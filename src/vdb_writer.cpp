#include "vdb_writer.hpp"

#include <openvdb/openvdb.h>

#include <cmath>
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

void add_grid_data(openvdb::FloatGrid &grid, const std::vector<float> &arr, int nx, int ny, int nz) {
  auto accessor = grid.getAccessor();
  for (int k = 0; k < nz; ++k) {
    for (int j = 0; j < ny; ++j) {
      for (int i = 0; i < nx; ++i) {
        const float v = arr[idx_fortran(i, j, k, nx, ny, nz)];
        if (v != 0.0f) {
          accessor.setValue(openvdb::Coord(i, j, k), v);
        }
      }
    }
  }
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

void write_vdb(
    const std::filesystem::path &filepath,
    const std::vector<float> &temperature,
    const std::vector<float> &density,
    int nx, int ny, int nz) {
  openvdb::initialize();

  auto temp_grid = openvdb::FloatGrid::create(/*background=*/0.0f);
  temp_grid->setName("temperature");
  temp_grid->setTransform(openvdb::math::Transform::createLinearTransform(1.0));

  auto dens_grid = openvdb::FloatGrid::create(/*background=*/0.0f);
  dens_grid->setName("density");
  dens_grid->setTransform(openvdb::math::Transform::createLinearTransform(1.0));

  add_grid_data(*temp_grid, temperature, nx, ny, nz);
  add_grid_data(*dens_grid, density, nx, ny, nz);

  openvdb::io::File file(filepath.string());
  openvdb::GridPtrVec grids;
  grids.push_back(temp_grid);
  grids.push_back(dens_grid);
  file.write(grids);
  file.close();
}

void write_manifest(
    const std::filesystem::path &filepath,
    const std::string &chid,
    int mesh_index_1based,
    const std::vector<double> &x,
    const std::vector<double> &y,
    const std::vector<double> &z,
    const std::vector<ManifestFrameInfo> &frames) {
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
  out << "  \"density_units\": \"kg_per_m3_proxy_from_s3d_decode\",\n";
  out << "  \"ambient_c\": 20.0,\n";
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
    out << "      \"density_max\": " << fr.density_max << "\n";
    out << "    }" << (i + 1 < frames.size() ? "," : "") << "\n";
  }

  out << "  ]\n";
  out << "}\n";
}

}  // namespace bsmv
