#include "s3d_reader.hpp"
#include "smv_parser.hpp"
#include "vdb_writer.hpp"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs = std::filesystem;

namespace {

struct Args {
  std::string chid;
  fs::path smv_path;
  fs::path result_dir = ".";
  fs::path out_dir = "vdb_sequence";
  std::string temperature_quantity = "EFFECTIVE FLAME TEMPERATURE";
  std::string density_quantity = "SOOT DENSITY";
  std::optional<int> start;
  std::optional<int> stop;
  int stride = 1;
  std::set<int> mesh_ids;
  int progress_every = 10;
  bool quiet = false;
};

void status(const Args &args, const std::string &msg) {
  if (!args.quiet) std::cout << msg << std::endl;
}

std::set<int> parse_mesh_ids(const std::string &text) {
  std::set<int> out;
  std::stringstream ss(text);
  std::string part;
  while (std::getline(ss, part, ',')) {
    if (part.empty()) continue;
    out.insert(std::stoi(part));
  }
  return out;
}

Args parse_args(int argc, char **argv) {
  Args args;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    auto need = [&](const std::string &name) -> std::string {
      if (i + 1 >= argc) throw std::runtime_error("Missing value for " + name);
      return argv[++i];
    };

    if (a == "--chid") args.chid = need(a);
    else if (a == "--smv") args.smv_path = need(a);
    else if (a == "--result-dir") args.result_dir = need(a);
    else if (a == "--out-dir") args.out_dir = need(a);
    else if (a == "--temperature-quantity") args.temperature_quantity = need(a);
    else if (a == "--density-quantity") args.density_quantity = need(a);
    else if (a == "--start") args.start = std::stoi(need(a));
    else if (a == "--stop") args.stop = std::stoi(need(a));
    else if (a == "--stride") args.stride = std::max(1, std::stoi(need(a)));
    else if (a == "--mesh-ids") args.mesh_ids = parse_mesh_ids(need(a));
    else if (a == "--progress-every") args.progress_every = std::max(1, std::stoi(need(a)));
    else if (a == "--quiet") args.quiet = true;
    else if (a == "--help" || a == "-h") {
      std::cout <<
        "Usage: bsmv --chid CHID --smv path/to/case.smv [options]\n"
        "  --result-dir DIR\n"
        "  --out-dir DIR\n"
        "  --temperature-quantity NAME\n"
        "  --density-quantity NAME\n"
        "  --start N\n"
        "  --stop N\n"
        "  --stride N\n"
        "  --mesh-ids 1,2,7\n"
        "  --progress-every N\n"
        "  --quiet\n";
      std::exit(0);
    } else {
      throw std::runtime_error("Unknown argument: " + a);
    }
  }
  if (args.chid.empty()) throw std::runtime_error("--chid is required");
  if (args.smv_path.empty()) throw std::runtime_error("--smv is required");
  return args;
}

std::string zero4(int n) {
  std::ostringstream oss;
  oss << std::setw(4) << std::setfill('0') << n;
  return oss.str();
}

bool nearly_equal(double a, double b, double atol = 1.0e-4) {
  return std::abs(a - b) <= atol;
}

}  // namespace

int main(int argc, char **argv) {
  try {
    const Args args = parse_args(argc, argv);

    status(args, "Parsing SMV: " + args.smv_path.string());
    const bsmv::SmvData smv = bsmv::parse_smv_file(args.smv_path);

    auto temp_by_mesh = bsmv::find_smokf3d_entries(smv, args.temperature_quantity);
    auto dens_by_mesh = bsmv::find_smokf3d_entries(smv, args.density_quantity);

    std::vector<int> meshes;
    for (const auto &kv : temp_by_mesh) {
      if (dens_by_mesh.find(kv.first) != dens_by_mesh.end()) {
        if (!args.mesh_ids.empty() && args.mesh_ids.find(kv.first) == args.mesh_ids.end()) continue;
        meshes.push_back(kv.first);
      }
    }
    std::sort(meshes.begin(), meshes.end());

    if (meshes.empty()) {
      throw std::runtime_error("No meshes have both requested smoke quantities.");
    }

    fs::create_directories(args.out_dir);
    status(args, "Writing VDB sequences to: " + fs::absolute(args.out_dir).string());

    constexpr float ambient_c = 20.0f;
    const float temp_min_smv = static_cast<float>(smv.temp_min);
    const float temp_max_smv = static_cast<float>(smv.temp_max);

    int mesh_counter = 0;
    for (const int mesh_id : meshes) {
      ++mesh_counter;
      const auto grid_it = smv.grids.find(mesh_id);
      if (grid_it == smv.grids.end()) {
        throw std::runtime_error("Missing grid metadata for mesh " + std::to_string(mesh_id));
      }
      const auto &grid = grid_it->second;
      const auto &temp_ent = temp_by_mesh.at(mesh_id);
      const auto &dens_ent = dens_by_mesh.at(mesh_id);

      const fs::path temp_path = args.result_dir / fs::path(temp_ent.filename).filename();
      const fs::path dens_path = args.result_dir / fs::path(dens_ent.filename).filename();
      const fs::path dens_sz_path = dens_path.string() + ".sz";

      status(args, "Starting mesh " + std::to_string(mesh_id) + " (" +
                       std::to_string(mesh_counter) + "/" + std::to_string(meshes.size()) + ")");
      status(args, "  temperature: " + temp_path.string());
      status(args, "  density    : " + dens_path.string());
      status(args, "  density sz : " + dens_sz_path.string());

      bsmv::S3dReader temp_reader(temp_path);
      bsmv::S3dReader dens_reader(dens_path);
      const auto dens_sizes = bsmv::read_s3d_size_file(dens_sz_path);

      std::vector<bsmv::ManifestFrameInfo> manifest_frames;

      bsmv::S3dFrame temp_frame, dens_frame;
      int iframe = 0;
      while (true) {
        const bool ok_t = temp_reader.next_frame(temp_frame);
        const bool ok_d = dens_reader.next_frame(dens_frame);
        if (ok_t != ok_d) {
          throw std::runtime_error("Temperature and density frame counts differ for mesh " + std::to_string(mesh_id));
        }
        if (!ok_t) break;

        if (iframe >= static_cast<int>(dens_sizes.size())) {
          throw std::runtime_error("Density .sz file has fewer frames than .s3d for mesh " + std::to_string(mesh_id));
        }
        const auto &dens_sz = dens_sizes[static_cast<std::size_t>(iframe)];
        if (dens_sz.nchars_in != dens_frame.nchars_in || dens_sz.nchars_out != dens_frame.nchars_out) {
          throw std::runtime_error("Density .sz nchars mismatch at frame " + std::to_string(iframe) +
                                   " for mesh " + std::to_string(mesh_id));
        }
        if (!nearly_equal(dens_sz.time, static_cast<double>(dens_frame.time))) {
          status(args, "WARNING: density .sz time mismatch at frame " + std::to_string(iframe) +
                           " for mesh " + std::to_string(mesh_id));
        }

        const bool take = (!args.start || iframe >= *args.start) &&
                          (!args.stop || iframe < *args.stop) &&
                          ((iframe - (args.start ? *args.start : 0)) % args.stride == 0);

        if (take) {
          auto temperature = bsmv::decode_temperature_excess_c(
              temp_frame.values, temp_min_smv, temp_max_smv, ambient_c);
          auto density = bsmv::decode_density_linear(dens_frame.values, dens_sz.max_val);

          const std::string out_name =
              args.chid + "_mesh_" + zero4(mesh_id) + "_frame_" + zero4(iframe) + ".vdb";
          const fs::path out_path = args.out_dir / out_name;
          bsmv::write_vdb(out_path, temperature, density, temp_reader.header().nx,
                          temp_reader.header().ny, temp_reader.header().nz);

          const auto [tmin, tmax] = bsmv::minmax(temperature);
          const auto [dmin, dmax] = bsmv::minmax(density);

          bsmv::ManifestFrameInfo info;
          info.frame_index = iframe;
          info.time = static_cast<double>(temp_frame.time);
          info.filename = out_name;
          info.temperature_min = tmin;
          info.temperature_max = tmax;
          info.density_min = dmin;
          info.density_max = dmax;
          manifest_frames.push_back(info);

          if (!args.quiet && (static_cast<int>(manifest_frames.size()) % args.progress_every == 0)) {
            std::cout << "    wrote " << manifest_frames.size()
                      << " frames for mesh " << mesh_id
                      << " (latest frame " << iframe << ", time " << temp_frame.time << ")"
                      << std::endl;
          }
        }

        ++iframe;
      }

      if (static_cast<std::size_t>(iframe) != dens_sizes.size()) {
        status(args, "WARNING: density .sz frame count differs from .s3d for mesh " + std::to_string(mesh_id));
      }

      const fs::path manifest_path = args.out_dir / (args.chid + "_mesh_" + zero4(mesh_id) + "_manifest.json");
      bsmv::write_manifest(
          manifest_path,
          args.chid,
          mesh_id,
          grid.x,
          grid.y,
          grid.z,
          manifest_frames,
          ambient_c,
          temp_min_smv,
          temp_max_smv,
          dens_ent.value);

      status(args, "Finished mesh " + std::to_string(mesh_id) +
                       " with " + std::to_string(manifest_frames.size()) + " written frames");
    }

    status(args, "Done.");
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "ERROR: " << e.what() << std::endl;
    return 1;
  }
}
