#include "geom_reader.hpp"
#include "s3d_reader.hpp"
#include "smv_parser.hpp"
#include "vdb_writer.hpp"

#include <openvdb/openvdb.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <csignal>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace fs = std::filesystem;

namespace {

volatile std::sig_atomic_t g_stop_requested = 0;

void handle_sigint(int) {
  g_stop_requested = 1;
}

struct Args {
  std::string chid;
  fs::path smv_path;
  fs::path result_dir = ".";
  fs::path out_dir = "vdb_sequence";
  fs::path geom_dir = "geometry";
  float temperature_cutoff = 20.0f;  // degC above ambient; 0 restores old near-all-nonzero behavior
  float density_cutoff = 1.0e-8f;    // kg/m3; 0 restores old near-all-nonzero behavior
  std::string temperature_quantity = "EFFECTIVE FLAME TEMPERATURE";
  std::string density_quantity = "SOOT DENSITY";
  std::optional<int> start;
  std::optional<int> stop;
  int stride = 1;
  std::set<int> mesh_ids;
  int progress_every = 10;
  int threads = 1;
  bool quiet = false;
  bool no_geom = false;
  bool geom_only = false;
};

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
    else if (a == "--geom-dir") args.geom_dir = need(a);
    else if (a == "--temperature-cutoff") args.temperature_cutoff = std::max(0.0f, std::stof(need(a)));
    else if (a == "--density-cutoff") args.density_cutoff = std::max(0.0f, std::stof(need(a)));
    else if (a == "--temperature-quantity") args.temperature_quantity = need(a);
    else if (a == "--density-quantity") args.density_quantity = need(a);
    else if (a == "--start") args.start = std::stoi(need(a));
    else if (a == "--stop") args.stop = std::stoi(need(a));
    else if (a == "--stride") args.stride = std::max(1, std::stoi(need(a)));
    else if (a == "--mesh-ids") args.mesh_ids = parse_mesh_ids(need(a));
    else if (a == "--progress-every") args.progress_every = std::max(1, std::stoi(need(a)));
    else if (a == "--threads") args.threads = std::max(1, std::stoi(need(a)));
    else if (a == "--quiet") args.quiet = true;
    else if (a == "--no-geom") args.no_geom = true;
    else if (a == "--geom-only") args.geom_only = true;
    else if (a == "--help" || a == "-h") {
      std::cout <<
        "Usage: bsmv --chid CHID --smv path/to/case.smv [options]\n"
        "  --result-dir DIR\n"
        "  --out-dir DIR       Directory for VDB output, default vdb_sequence\n"
        "  --geom-dir DIR      Directory for GEOM/OBJ output, default geometry\n"
        "  --temperature-cutoff C   Only activate VDB temperature voxels above C degC above ambient, default 20\n"
        "  --density-cutoff R       Only activate VDB density voxels above R kg/m3, default 1e-8\n"
        "                         Use 0 for either cutoff to restore old all-nonzero behavior\n"
        "  --temperature-quantity NAME\n"
        "  --density-quantity NAME\n"
        "  --start N\n"
        "  --stop N\n"
        "  --stride N\n"
        "  --mesh-ids 1,2,7\n"
        "  --progress-every N\n"
        "  --threads N\n"
        "  --no-geom       Do not read/write GEOM OBJ files\n"
        "  --geom-only     Write GEOM OBJ files, then stop before smoke/VDB conversion\n"
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

struct SharedState {
  std::mutex log_mutex;
  std::atomic<std::size_t> next_mesh_index{0};
  std::atomic<int> meshes_completed{0};
  std::atomic<int> meshes_started{0};
  std::atomic<int> meshes_failed{0};
};

void status(const Args &args, SharedState &shared, const std::string &msg) {
  if (args.quiet) return;
  std::lock_guard<std::mutex> lock(shared.log_mutex);
  std::cout << msg << std::endl;
}

std::string mesh_prefix(const std::string &tag, int mesh_id) {
  std::ostringstream oss;
  oss << "[" << tag << " mesh " << mesh_id << "] ";
  return oss.str();
}

fs::path first_existing_path(const std::vector<fs::path> &paths) {
  for (const auto &p : paths) {
    if (!p.empty() && fs::exists(p)) return p;
  }
  return {};
}

fs::path locate_ge_file(const Args &args, const bsmv::SmvData &smv) {
  const fs::path smv_dir = args.smv_path.parent_path();
  const std::string chid = !smv.chid.empty() ? smv.chid : args.chid;
  std::vector<fs::path> candidates;

  if (!smv.geom.ge_filename.empty()) {
    const fs::path from_smv = smv.geom.ge_filename;
    if (from_smv.is_absolute()) candidates.push_back(from_smv);
    candidates.push_back(args.result_dir / from_smv.filename());
    candidates.push_back(smv_dir / from_smv.filename());
    candidates.push_back(from_smv);
  }

  candidates.push_back(args.result_dir / (chid + "_1.ge"));
  candidates.push_back(smv_dir / (chid + "_1.ge"));

  return first_existing_path(candidates);
}

bool write_geometry_from_smv(const Args &args, SharedState &shared, const bsmv::SmvData &smv) {
  const fs::path ge_path = locate_ge_file(args, smv);
  if (ge_path.empty()) {
    if (smv.geom.n_geometry > 0 || args.geom_only) {
      throw std::runtime_error("SMV references GEOM data, but could not find the .ge file. "
                               "Looked beside --result-dir and beside the .smv file.");
    }
    status(args, shared, "[geom] no GEOM block or default CHID_1.ge file found; skipping geometry");
    return false;
  }

  fs::path ge2_path = ge_path;
  ge2_path.replace_extension(".ge2");
  if (!fs::exists(ge2_path)) {
    throw std::runtime_error("Found GE file but missing matching GE2 file: " + ge2_path.string());
  }

  fs::create_directories(args.geom_dir);

  status(args, shared, "[geom] writing OBJ files to: " + fs::absolute(args.geom_dir).string());

  status(args, shared, "[geom] reading GE : " + ge_path.string());
  status(args, shared, "[geom] reading GE2: " + ge2_path.string());

  auto meshes = bsmv::read_fds_ge_split_by_geom(
      ge_path,
      ge2_path,
      smv.geom.n_geometry,
      &smv.geom);

  std::vector<bsmv::GeomOutputInfo> manifest_items;

  for (const auto &gm : meshes) {
    if (gm.faces_1based.empty()) {
      status(args, shared, "[geom] GEOM " + std::to_string(gm.geom_index_1based) + " has no faces; skipping OBJ");
      continue;
    }

    const std::string obj_name =
        args.chid + "_geom_" + zero4(gm.geom_index_1based) + ".obj";
    const fs::path obj_path = args.geom_dir / obj_name;
    const bsmv::GeomSmvEntry *geom_entry = nullptr;
    if (gm.geom_index_1based >= 1 &&
        gm.geom_index_1based <= static_cast<int>(smv.geom.entries.size())) {
      geom_entry = &smv.geom.entries[static_cast<std::size_t>(gm.geom_index_1based - 1)];
    }
    bsmv::write_obj(obj_path, gm, smv.surfaces, geom_entry);

    bsmv::GeomOutputInfo info;
    info.geom_index_1based = gm.geom_index_1based;
    info.obj_filename = obj_name;
    info.n_vertices = static_cast<int>(gm.vertices.size());
    info.n_faces = static_cast<int>(gm.faces_1based.size());
    info.bbox = gm.bbox;
    manifest_items.push_back(info);

    status(args, shared,
           "[geom] wrote " + obj_path.string() +
           " vertices=" + std::to_string(info.n_vertices) +
           " faces=" + std::to_string(info.n_faces));
  }

  const fs::path manifest_path = args.geom_dir / "geometry_manifest.json";
  bsmv::write_geometry_manifest(manifest_path, args.chid, ge_path, ge2_path, manifest_items);
  status(args, shared, "[geom] wrote " + manifest_path.string());

  const fs::path scene_manifest_path = args.geom_dir / "scene_manifest.json";
  bsmv::write_scene_manifest(scene_manifest_path, smv);
  status(args, shared, "[scene] wrote " + scene_manifest_path.string());

  return true;
}

bool select_density_max(
    const bsmv::S3dSizeFrame &sz,
    const bsmv::S3dFrame &dens_frame,
    float &chosen_max,
    std::string &why)
{
  if (sz.has_primary &&
      sz.nchars_in == dens_frame.nchars_in &&
      sz.nchars_out == dens_frame.nchars_out) {
    chosen_max = sz.max_val;
    why = "primary";
    return true;
  }

  if (sz.has_secondary &&
      sz.nchars_in == dens_frame.nchars_in &&
      sz.nchars_out2 == dens_frame.nchars_out) {
    chosen_max = sz.max_val2;
    why = "secondary";
    return true;
  }

  return false;
}

bool process_mesh(
    const Args &args,
    SharedState &shared,
    const bsmv::SmvData &smv,
    const std::unordered_map<int, bsmv::SmokeFileEntry> &temp_by_mesh,
    const std::unordered_map<int, bsmv::SmokeFileEntry> &dens_by_mesh,
    int mesh_id)
{
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

  status(args, shared, mesh_prefix("start", mesh_id) + "temperature: " + temp_path.string());
  status(args, shared, mesh_prefix("start", mesh_id) + "density    : " + dens_path.string());
  status(args, shared, mesh_prefix("start", mesh_id) + "density sz : " + dens_sz_path.string());

  bsmv::S3dReader temp_reader(temp_path);
  bsmv::S3dReader dens_reader(dens_path);
  const auto dens_sizes = bsmv::read_s3d_size_file(dens_sz_path);

  std::vector<bsmv::ManifestFrameInfo> manifest_frames;
  if (args.start && args.stop && *args.stop > *args.start && args.stride > 0) {
    const auto nsel = std::max(0, (*args.stop - *args.start + args.stride - 1) / args.stride);
    manifest_frames.reserve(static_cast<std::size_t>(nsel));
  }

  constexpr float ambient_c = 20.0f;
  const float temp_min_smv = static_cast<float>(smv.temp_min);
  const float temp_max_smv = static_cast<float>(smv.temp_max);

  bsmv::S3dFrame temp_frame, dens_frame;
  int iframe = 0;
  while (true) {
    if (g_stop_requested) break;

    const bool ok_t = temp_reader.next_frame(temp_frame);
    const bool ok_d = dens_reader.next_frame(dens_frame);

    if (!ok_t || !ok_d) {
      if (ok_t != ok_d) {
        status(args, shared, mesh_prefix("warn", mesh_id) +
                                 "temperature/density frame counts differ; ending mesh early");
      }
      break;
    }

    if (iframe >= static_cast<int>(dens_sizes.size())) {
      status(args, shared, mesh_prefix("warn", mesh_id) +
                               "density .sz ended at frame " + std::to_string(iframe) +
                               "; ending mesh early");
      break;
    }

    const auto &dens_sz = dens_sizes[static_cast<std::size_t>(iframe)];

    float dens_max = 0.0f;
    std::string sz_choice;
    if (!select_density_max(dens_sz, dens_frame, dens_max, sz_choice)) {
      std::ostringstream oss;
      oss << "density .sz mismatch at frame " << iframe
          << " (binary nchars_in=" << dens_frame.nchars_in
          << ", nchars_out=" << dens_frame.nchars_out
          << "; sz primary="
          << (dens_sz.has_primary ? std::to_string(dens_sz.nchars_out) : std::string("none"))
          << ", secondary="
          << (dens_sz.has_secondary ? std::to_string(dens_sz.nchars_out2) : std::string("none"))
          << "); ending mesh early";
      status(args, shared, mesh_prefix("warn", mesh_id) + oss.str());
      break;
    }

    if (!nearly_equal(dens_sz.time, static_cast<double>(dens_frame.time))) {
      status(args, shared, mesh_prefix("warn", mesh_id) +
                               "density .sz time mismatch at frame " + std::to_string(iframe) +
                               " using " + sz_choice + " pair");
    }

    const bool take = (!args.start || iframe >= *args.start) &&
                      (!args.stop || iframe < *args.stop) &&
                      ((iframe - (args.start ? *args.start : 0)) % args.stride == 0);

    if (take) {
      auto temperature = bsmv::decode_temperature_excess_c(
          temp_frame.values, temp_min_smv, temp_max_smv, ambient_c);
      auto density = bsmv::decode_density_linear(dens_frame.values, dens_max);

      const std::string out_name =
          args.chid + "_mesh_" + zero4(mesh_id) + "_frame_" + zero4(iframe) + ".vdb";
      const fs::path out_path = args.out_dir / out_name;
      const bsmv::VdbThresholdOptions vdb_opts{args.temperature_cutoff, args.density_cutoff};
      const bsmv::VdbWriteStats vdb_stats = bsmv::write_vdb(
          out_path, temperature, density, temp_reader.header().nx,
          temp_reader.header().ny, temp_reader.header().nz, vdb_opts);

      // With thresholded sparse output, a selected frame may have no active
      // temperature or density voxels. In that case write_vdb intentionally
      // does not create a .vdb file, and we must not add it to the manifest.
      // Otherwise Blender imports a VOLUME object whose obj.data.grids is empty.
      if (!vdb_stats.wrote_file) {
        if (!args.quiet && args.progress_every > 0 && (iframe % args.progress_every == 0)) {
          status(args, shared,
                 mesh_prefix("skip", mesh_id) +
                 "frame " + std::to_string(iframe) +
                 " has no active voxels after thresholding");
        }
      } else {
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
        info.temperature_active_voxels = vdb_stats.temperature_active_voxels;
        info.density_active_voxels = vdb_stats.density_active_voxels;
        manifest_frames.push_back(info);

        if (!args.quiet && (static_cast<int>(manifest_frames.size()) % args.progress_every == 0)) {
          status(args, shared,
                 mesh_prefix("prog", mesh_id) +
                 "wrote " + std::to_string(manifest_frames.size()) +
                 " non-empty frames (latest frame " + std::to_string(iframe) +
                 ", time " + std::to_string(temp_frame.time) +
                 ", active T/D " + std::to_string(vdb_stats.temperature_active_voxels) +
                 "/" + std::to_string(vdb_stats.density_active_voxels) + ")");
        }
      }
    }

    ++iframe;
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
      dens_ent.value,
      args.temperature_cutoff,
      args.density_cutoff);

  status(args, shared, mesh_prefix("done", mesh_id) +
                           "finished with " + std::to_string(manifest_frames.size()) + " written frames");
  return true;
}

}  // namespace

int main(int argc, char **argv) {
  try {
    const Args args = parse_args(argc, argv);

    std::signal(SIGINT, handle_sigint);

    SharedState shared;

    status(args, shared, "Parsing SMV: " + args.smv_path.string());
    const bsmv::SmvData smv = bsmv::parse_smv_file(args.smv_path);

    if (!args.no_geom) {
      write_geometry_from_smv(args, shared, smv);
    }

    if (args.geom_only) {
      status(args, shared, "Done with --geom-only.");
      return 0;
    }

    fs::create_directories(args.out_dir);

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

    status(args, shared, "Writing VDB sequences to: " + fs::absolute(args.out_dir).string());
    status(args, shared, "VDB activation cutoffs: temperature > " +
                             std::to_string(args.temperature_cutoff) +
                             " degC above ambient, density > " +
                             std::to_string(args.density_cutoff) + " kg/m3");

    openvdb::initialize();

    const unsigned hc = std::thread::hardware_concurrency();
    int threads = std::max(1, args.threads);
    if (!meshes.empty()) {
      threads = std::min<int>(threads, static_cast<int>(meshes.size()));
    }
    if (hc > 0) {
      threads = std::min<int>(threads, static_cast<int>(hc));
    }

    status(args, shared, "Using " + std::to_string(threads) + " thread(s) over " +
                             std::to_string(meshes.size()) + " mesh(es)");

    auto worker = [&](int worker_id) {
      (void)worker_id;
      while (!g_stop_requested) {
        const std::size_t idx = shared.next_mesh_index.fetch_add(1);
        if (idx >= meshes.size()) break;

        const int mesh_id = meshes[idx];
        shared.meshes_started.fetch_add(1);

        try {
          process_mesh(args, shared, smv, temp_by_mesh, dens_by_mesh, mesh_id);
          shared.meshes_completed.fetch_add(1);
        } catch (const std::exception &e) {
          shared.meshes_failed.fetch_add(1);
          status(args, shared, mesh_prefix("fail", mesh_id) + e.what());
          continue;
        }
      }
    };

    std::vector<std::thread> workers;
    workers.reserve(static_cast<std::size_t>(threads));
    for (int i = 0; i < threads; ++i) {
      workers.emplace_back(worker, i);
    }
    for (auto &t : workers) {
      t.join();
    }

    if (g_stop_requested) {
      status(args, shared, "Interrupted. Started " + std::to_string(shared.meshes_started.load()) +
                               ", completed " + std::to_string(shared.meshes_completed.load()) +
                               ", failed " + std::to_string(shared.meshes_failed.load()) + ".");
      return shared.meshes_failed.load() > 0 ? 1 : 130;
    }

    status(args, shared, "Done. Started " + std::to_string(shared.meshes_started.load()) +
                             ", completed " + std::to_string(shared.meshes_completed.load()) +
                             ", failed " + std::to_string(shared.meshes_failed.load()) + ".");
    return shared.meshes_failed.load() > 0 ? 1 : 0;
  } catch (const std::exception &e) {
    std::cerr << "ERROR: " << e.what() << std::endl;
    return 1;
  }
}
