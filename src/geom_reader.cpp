#include "geom_reader.hpp"

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <unordered_map>

namespace bsmv {

namespace {

struct FortranRecordReader {
  std::filesystem::path path;
  std::ifstream in;

  explicit FortranRecordReader(const std::filesystem::path &p)
      : path(p), in(p, std::ios::binary) {
    if (!in) throw std::runtime_error("Could not open file: " + p.string());
  }

  bool eof() {
    return in.peek() == std::ifstream::traits_type::eof();
  }

  std::vector<char> read_record() {
    std::int32_t n1 = 0;
    in.read(reinterpret_cast<char *>(&n1), sizeof(n1));
    if (!in) throw std::runtime_error("Unexpected EOF while reading Fortran record in " + path.string());
    if (n1 < 0) throw std::runtime_error("Negative Fortran record length in " + path.string());

    std::vector<char> data(static_cast<std::size_t>(n1));
    if (n1 > 0) in.read(data.data(), static_cast<std::streamsize>(n1));

    std::int32_t n2 = 0;
    in.read(reinterpret_cast<char *>(&n2), sizeof(n2));
    if (!in || n1 != n2) {
      throw std::runtime_error("Bad Fortran record marker in " + path.string());
    }
    return data;
  }

  template <class T>
  T scalar() {
    const auto r = read_record();
    if (r.size() != sizeof(T)) {
      std::ostringstream oss;
      oss << "Unexpected scalar record size in " << path.string()
          << ": got " << r.size() << ", expected " << sizeof(T);
      throw std::runtime_error(oss.str());
    }
    T v{};
    std::memcpy(&v, r.data(), sizeof(T));
    return v;
  }

  template <class T>
  std::vector<T> array(std::size_t n) {
    const auto r = read_record();
    const std::size_t expected = n * sizeof(T);
    if (r.size() != expected) {
      std::ostringstream oss;
      oss << "Unexpected array record size in " << path.string()
          << ": got " << r.size() << ", expected " << expected;
      throw std::runtime_error(oss.str());
    }
    std::vector<T> out(n);
    if (!out.empty()) std::memcpy(out.data(), r.data(), r.size());
    return out;
  }
};

std::string json_escape(const std::string &s) {
  std::ostringstream out;
  for (const char c : s) {
    switch (c) {
      case '\\': out << "\\\\"; break;
      case '"': out << "\\\""; break;
      case '\n': out << "\\n"; break;
      case '\r': out << "\\r"; break;
      case '\t': out << "\\t"; break;
      default: out << c; break;
    }
  }
  return out.str();
}

void update_bbox(std::array<double, 6> &bbox, const std::array<float, 3> &v, bool first) {
  if (first) {
    bbox = {v[0], v[0], v[1], v[1], v[2], v[2]};
    return;
  }
  bbox[0] = std::min<double>(bbox[0], v[0]);
  bbox[1] = std::max<double>(bbox[1], v[0]);
  bbox[2] = std::min<double>(bbox[2], v[1]);
  bbox[3] = std::max<double>(bbox[3], v[1]);
  bbox[4] = std::min<double>(bbox[4], v[2]);
  bbox[5] = std::max<double>(bbox[5], v[2]);
}

}  // namespace

std::vector<GeomTriMesh> read_fds_ge_split_by_geom(
    const std::filesystem::path &ge_path,
    const std::filesystem::path &ge2_path,
    int n_geometry_hint,
    const GeomSmvBlock *smv_geom) {
  FortranRecordReader ge(ge_path);
  FortranRecordReader ge2(ge2_path);

  const int one = ge.scalar<int>();
  const int version = ge.scalar<int>();
  const auto geom_header = ge.array<int>(3);
  (void)geom_header;

  if (one != 1) {
    throw std::runtime_error("Unsupported GE file: first record is not 1 in " + ge_path.string());
  }
  if (version != 2) {
    throw std::runtime_error("Unsupported GE file version " + std::to_string(version) +
                             " in " + ge_path.string());
  }

  // First frame is the static GEOM frame. Dynamic frames may follow; this first pass ignores them.
  const float time = ge.scalar<float>();
  (void)time;

  const auto counts = ge.array<int>(3);
  if (counts.size() != 3) throw std::runtime_error("Bad GE count record in " + ge_path.string());
  const int nverts = counts[0];
  const int nfaces = counts[1];
  const int nvolus = counts[2];
  if (nverts < 0 || nfaces < 0 || nvolus < 0) {
    throw std::runtime_error("Negative GE counts in " + ge_path.string());
  }

  auto verts_flat = nverts > 0 ? ge.array<float>(static_cast<std::size_t>(3 * nverts))
                               : std::vector<float>{};
  auto faces_flat = nfaces > 0 ? ge.array<int>(static_cast<std::size_t>(3 * nfaces))
                               : std::vector<int>{};
  auto surf_ids = nfaces > 0 ? ge.array<int>(static_cast<std::size_t>(nfaces))
                             : std::vector<int>{};

  if (nfaces > 0) {
    auto tfaces = ge.array<float>(static_cast<std::size_t>(6 * nfaces));
    (void)tfaces;
  }
  if (nvolus > 0) {
    auto volus = ge.array<int>(static_cast<std::size_t>(4 * nvolus));
    auto matl_ids = ge.array<int>(static_cast<std::size_t>(nvolus));
    (void)volus;
    (void)matl_ids;
  }

  const int nfaces2 = ge2.scalar<int>();
  if (nfaces2 != nfaces) {
    std::ostringstream oss;
    oss << ".ge2 face count mismatch for " << ge2_path.string()
        << ": got " << nfaces2 << ", expected " << nfaces;
    throw std::runtime_error(oss.str());
  }
  auto geom_ids = nfaces > 0 ? ge2.array<int>(static_cast<std::size_t>(nfaces))
                             : std::vector<int>{};

  int max_geom_id = n_geometry_hint;
  for (const int gid : geom_ids) max_geom_id = std::max(max_geom_id, gid);
  if (max_geom_id < 1) max_geom_id = 1;

  std::vector<GeomTriMesh> out(static_cast<std::size_t>(max_geom_id));
  std::vector<std::unordered_map<int, int>> global_to_local(static_cast<std::size_t>(max_geom_id));
  std::vector<bool> bbox_initialized(static_cast<std::size_t>(max_geom_id), false);

  for (int g = 0; g < max_geom_id; ++g) {
    out[static_cast<std::size_t>(g)].geom_index_1based = g + 1;
    if (smv_geom && g < static_cast<int>(smv_geom->entries.size())) {
      out[static_cast<std::size_t>(g)].bbox = smv_geom->entries[static_cast<std::size_t>(g)].bbox;
    }
  }

  auto get_local_vertex = [&](int gid_1based, int global_1based) -> int {
    if (global_1based < 1 || global_1based > nverts) {
      throw std::runtime_error("GE face references vertex outside valid range in " + ge_path.string());
    }

    const std::size_t gidx = static_cast<std::size_t>(gid_1based - 1);
    auto &map = global_to_local[gidx];
    const auto it = map.find(global_1based);
    if (it != map.end()) return it->second;

    const int k = global_1based - 1;
    std::array<float, 3> v = {
        verts_flat[static_cast<std::size_t>(3 * k + 0)],
        verts_flat[static_cast<std::size_t>(3 * k + 1)],
        verts_flat[static_cast<std::size_t>(3 * k + 2)]};

    auto &mesh = out[gidx];
    mesh.vertices.push_back(v);
    const int local_1based = static_cast<int>(mesh.vertices.size());
    map.emplace(global_1based, local_1based);

    update_bbox(mesh.bbox, v, !bbox_initialized[gidx]);
    bbox_initialized[gidx] = true;
    return local_1based;
  };

  for (int f = 0; f < nfaces; ++f) {
    const int gid = geom_ids[static_cast<std::size_t>(f)];
    if (gid < 1 || gid > max_geom_id) continue;

    const int i1 = faces_flat[static_cast<std::size_t>(3 * f + 0)];
    const int i2 = faces_flat[static_cast<std::size_t>(3 * f + 1)];
    const int i3 = faces_flat[static_cast<std::size_t>(3 * f + 2)];

    auto &mesh = out[static_cast<std::size_t>(gid - 1)];
    mesh.faces_1based.push_back({
        get_local_vertex(gid, i1),
        get_local_vertex(gid, i2),
        get_local_vertex(gid, i3)});
    mesh.surf_ids.push_back(surf_ids.empty() ? 0 : surf_ids[static_cast<std::size_t>(f)]);
  }

  return out;
}

void write_obj(const std::filesystem::path &path, const GeomTriMesh &mesh) {
  std::ofstream out(path);
  if (!out) throw std::runtime_error("Could not write OBJ file: " + path.string());

  out << "# bsmv GEOM " << mesh.geom_index_1based << "\n";
  out << "o geom_" << std::setw(4) << std::setfill('0') << mesh.geom_index_1based << std::setfill(' ') << "\n";

  for (const auto &v : mesh.vertices) {
    out << std::setprecision(9) << "v " << v[0] << " " << v[1] << " " << v[2] << "\n";
  }

  int last_surf = -999999;
  for (std::size_t i = 0; i < mesh.faces_1based.size(); ++i) {
    const int surf = i < mesh.surf_ids.size() ? mesh.surf_ids[i] : 0;
    if (surf != last_surf) {
      out << "g geom_" << std::setw(4) << std::setfill('0') << mesh.geom_index_1based
          << "_surf_" << surf << std::setfill(' ') << "\n";
      last_surf = surf;
    }
    const auto &f = mesh.faces_1based[i];
    out << "f " << f[0] << " " << f[1] << " " << f[2] << "\n";
  }
}

void write_geometry_manifest(
    const std::filesystem::path &path,
    const std::string &chid,
    const std::filesystem::path &ge_path,
    const std::filesystem::path &ge2_path,
    const std::vector<GeomOutputInfo> &items) {
  std::ofstream out(path);
  if (!out) throw std::runtime_error("Could not write geometry manifest: " + path.string());

  out << std::fixed << std::setprecision(9);
  out << "{\n";
  out << "  \"chid\": \"" << json_escape(chid) << "\",\n";
  out << "  \"format\": \"bsmv_geometry_obj_v1\",\n";
  out << "  \"source_ge\": \"" << json_escape(ge_path.string()) << "\",\n";
  out << "  \"source_ge2\": \"" << json_escape(ge2_path.string()) << "\",\n";
  out << "  \"geometry\": [\n";

  for (std::size_t i = 0; i < items.size(); ++i) {
    const auto &it = items[i];
    out << "    {\n";
    out << "      \"geom_index_1based\": " << it.geom_index_1based << ",\n";
    out << "      \"filename\": \"" << json_escape(it.obj_filename) << "\",\n";
    out << "      \"n_vertices\": " << it.n_vertices << ",\n";
    out << "      \"n_faces\": " << it.n_faces << ",\n";
    out << "      \"bbox\": [" << it.bbox[0] << ", " << it.bbox[1] << ", "
        << it.bbox[2] << ", " << it.bbox[3] << ", "
        << it.bbox[4] << ", " << it.bbox[5] << "]\n";
    out << "    }" << (i + 1 < items.size() ? "," : "") << "\n";
  }

  out << "  ]\n";
  out << "}\n";
}

}  // namespace bsmv
