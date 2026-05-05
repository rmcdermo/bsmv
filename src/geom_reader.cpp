#include "geom_reader.hpp"

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <map>
#include <set>
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

std::string upper_ascii(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(),
                 [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
  return s;
}

std::string material_safe_token(const std::string &text) {
  std::string out;
  out.reserve(text.size());
  for (const unsigned char c : text) {
    if (std::isalnum(c) || c == '_' || c == '-' || c == '.') {
      out.push_back(static_cast<char>(c));
    } else if (std::isspace(c)) {
      out.push_back('_');
    }
  }
  if (out.empty()) out = "unnamed";
  return out;
}

std::string geom_surface_hint(const GeomSmvEntry *entry) {
  if (!entry) return {};
  const std::string &line = entry->metadata_line;
  const auto pct = line.find('%');
  if (pct == std::string::npos) return {};
  const auto bang = line.find('!', pct + 1);
  if (bang == std::string::npos) return trim(line.substr(pct + 1));
  return trim(line.substr(pct + 1, bang - pct - 1));
}

const SurfaceInfo *surface_by_index(const std::vector<SurfaceInfo> &surfaces, int index) {
  for (const auto &sf : surfaces) {
    if (sf.surface_index == index) return &sf;
  }
  if (index >= 0 && index < static_cast<int>(surfaces.size())) {
    return &surfaces[static_cast<std::size_t>(index)];
  }
  return nullptr;
}

const SurfaceInfo *surface_by_id(const std::vector<SurfaceInfo> &surfaces, const std::string &id) {
  const std::string want = upper_ascii(trim(id));
  if (want.empty()) return nullptr;
  for (const auto &sf : surfaces) {
    if (upper_ascii(trim(sf.id)) == want) return &sf;
  }
  return nullptr;
}

bool same_surface(const SurfaceInfo *a, const SurfaceInfo *b) {
  if (!a || !b) return false;
  return upper_ascii(trim(a->id)) == upper_ascii(trim(b->id));
}

const SurfaceInfo *choose_surface_for_surf_id(
    int surf_id,
    const std::string &geom_hint,
    const std::vector<SurfaceInfo> &surfaces) {
  const SurfaceInfo *fallback = surface_by_id(surfaces, geom_hint);

  if (surf_id < 0) return fallback;

  const SurfaceInfo *exact = surface_by_index(surfaces, surf_id);
  const SurfaceInfo *one_based = surf_id > 0 ? surface_by_index(surfaces, surf_id - 1) : nullptr;

  // The .ge SURF indices normally match the 0-based SURFACE order in the .smv
  // file. This fallback logic keeps us robust if a file stores a one-based index
  // on a GEOM whose metadata line identifies its default SURF_ID.
  if (fallback && same_surface(exact, fallback)) return exact;
  if (fallback && same_surface(one_based, fallback)) return one_based;

  if (exact) return exact;
  if (one_based && !exact) return one_based;
  return fallback;
}

struct MaterialRecord {
  std::string name;
  std::string label;
  int surf_id = -999999;
  std::array<double, 3> rgb = {0.7, 0.7, 0.7};
  double alpha = 1.0;
};

MaterialRecord material_record_for_face(
    int surf_id,
    const std::string &geom_hint,
    const std::vector<SurfaceInfo> &surfaces) {
  MaterialRecord rec;
  rec.surf_id = surf_id;

  const SurfaceInfo *sf = choose_surface_for_surf_id(surf_id, geom_hint, surfaces);
  if (sf) {
    rec.label = sf->id;
    rec.rgb = sf->rgb;
    rec.alpha = sf->transparency;
    rec.name = "surf_" + std::to_string(sf->surface_index) + "_" + material_safe_token(sf->id);
  } else if (!trim(geom_hint).empty()) {
    rec.label = geom_hint;
    rec.name = "geom_hint_" + material_safe_token(geom_hint);
  } else {
    rec.label = "surf_id_" + std::to_string(surf_id);
    rec.name = "surf_id_" + std::to_string(surf_id);
  }
  return rec;
}

void write_mtl_file(const std::filesystem::path &path, const std::map<std::string, MaterialRecord> &materials) {
  std::ofstream out(path);
  if (!out) throw std::runtime_error("Could not write MTL file: " + path.string());

  out << std::fixed << std::setprecision(9);
  out << "# bsmv GEOM materials from SMV SURFACE blocks\n";
  for (const auto &kv : materials) {
    const auto &mat = kv.second;
    const double alpha = std::max(0.0, std::min(1.0, mat.alpha));
    out << "\nnewmtl " << mat.name << "\n";
    out << "# label " << mat.label << "\n";
    out << "# surf_id " << mat.surf_id << "\n";
    out << "Ka " << mat.rgb[0] << " " << mat.rgb[1] << " " << mat.rgb[2] << "\n";
    out << "Kd " << mat.rgb[0] << " " << mat.rgb[1] << " " << mat.rgb[2] << "\n";
    out << "Ks 0.000000000 0.000000000 0.000000000\n";
    out << "d " << alpha << "\n";
    out << "Tr " << (1.0 - alpha) << "\n";
    out << "illum 2\n";
  }
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

void write_obj(
    const std::filesystem::path &path,
    const GeomTriMesh &mesh,
    const std::vector<SurfaceInfo> &surfaces,
    const GeomSmvEntry *geom_entry) {
  std::ofstream out(path);
  if (!out) throw std::runtime_error("Could not write OBJ file: " + path.string());

  const std::string geom_hint = geom_surface_hint(geom_entry);

  std::vector<std::string> face_materials;
  face_materials.reserve(mesh.faces_1based.size());
  std::map<std::string, MaterialRecord> materials;
  for (std::size_t i = 0; i < mesh.faces_1based.size(); ++i) {
    const int surf_id = i < mesh.surf_ids.size() ? mesh.surf_ids[i] : -1;
    MaterialRecord rec = material_record_for_face(surf_id, geom_hint, surfaces);
    face_materials.push_back(rec.name);
    materials.emplace(rec.name, rec);
  }

  std::filesystem::path mtl_filename = path.filename();
  mtl_filename.replace_extension(".mtl");
  const std::filesystem::path mtl_path = path.parent_path() / mtl_filename;
  write_mtl_file(mtl_path, materials);

  out << "# bsmv GEOM " << mesh.geom_index_1based << "\n";
  if (!geom_hint.empty()) out << "# geom_surface_hint " << geom_hint << "\n";
  out << "mtllib " << mtl_filename.string() << "\n";
  out << "o geom_" << std::setw(4) << std::setfill('0') << mesh.geom_index_1based << std::setfill(' ') << "\n";

  for (const auto &v : mesh.vertices) {
    out << std::setprecision(9) << "v " << v[0] << " " << v[1] << " " << v[2] << "\n";
  }

  std::string last_material;
  int last_surf = -999999;
  for (std::size_t i = 0; i < mesh.faces_1based.size(); ++i) {
    const int surf = i < mesh.surf_ids.size() ? mesh.surf_ids[i] : -1;
    const std::string &mat = face_materials[i];
    if (mat != last_material || surf != last_surf) {
      out << "g geom_" << std::setw(4) << std::setfill('0') << mesh.geom_index_1based
          << "_surf_" << surf << std::setfill(' ') << "\n";
      out << "# surf_id " << surf << " material " << mat << "\n";
      out << "usemtl " << mat << "\n";
      last_material = mat;
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


namespace {

std::array<double, 6> mesh_bbox(const MeshGrid &g) {
  if (g.x.empty() || g.y.empty() || g.z.empty()) {
    return {0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
  }
  return {g.x.front(), g.x.back(), g.y.front(), g.y.back(), g.z.front(), g.z.back()};
}

std::array<double, 6> domain_bbox_from_grids(const std::map<int, MeshGrid> &grids) {
  bool first = true;
  std::array<double, 6> bbox = {0.0, 0.0, 0.0, 0.0, 0.0, 0.0};
  for (const auto &kv : grids) {
    const auto b = mesh_bbox(kv.second);
    if (first) {
      bbox = b;
      first = false;
    } else {
      bbox[0] = std::min(bbox[0], b[0]);
      bbox[1] = std::max(bbox[1], b[1]);
      bbox[2] = std::min(bbox[2], b[2]);
      bbox[3] = std::max(bbox[3], b[3]);
      bbox[4] = std::min(bbox[4], b[4]);
      bbox[5] = std::max(bbox[5], b[5]);
    }
  }
  return bbox;
}

void write_bbox_json(std::ostream &out, const std::array<double, 6> &b) {
  out << "[" << b[0] << ", " << b[1] << ", "
      << b[2] << ", " << b[3] << ", "
      << b[4] << ", " << b[5] << "]";
}

void write_rgb_json(std::ostream &out, const std::array<double, 3> &rgb) {
  out << "[" << rgb[0] << ", " << rgb[1] << ", " << rgb[2] << "]";
}

}  // namespace

void write_scene_manifest(
    const std::filesystem::path &path,
    const SmvData &smv) {
  std::ofstream out(path);
  if (!out) throw std::runtime_error("Could not write scene manifest: " + path.string());

  const auto domain_bbox = domain_bbox_from_grids(smv.grids);

  out << std::fixed << std::setprecision(9);
  out << "{\n";
  out << "  \"format\": \"bsmv_scene_v1\",\n";
  out << "  \"chid\": \"" << json_escape(smv.chid) << "\",\n";
  out << "  \"default_surface_id\": \"" << json_escape(smv.default_surface_id) << "\",\n";
  out << "  \"domain_bbox\": ";
  write_bbox_json(out, domain_bbox);
  out << ",\n";

  out << "  \"meshes\": [\n";
  std::size_t mesh_i = 0;
  for (const auto &kv : smv.grids) {
    const auto &g = kv.second;
    out << "    {\n";
    out << "      \"mesh_index_1based\": " << g.mesh_index_1based << ",\n";
    out << "      \"ibar\": " << g.ibar << ", \"jbar\": " << g.jbar << ", \"kbar\": " << g.kbar << ",\n";
    out << "      \"bbox\": ";
    write_bbox_json(out, mesh_bbox(g));
    out << "\n";
    out << "    }" << (++mesh_i < smv.grids.size() ? "," : "") << "\n";
  }
  out << "  ],\n";

  out << "  \"surfaces\": [\n";
  for (std::size_t i = 0; i < smv.surfaces.size(); ++i) {
    const auto &sf = smv.surfaces[i];
    out << "    {\n";
    out << "      \"surface_index\": " << sf.surface_index << ",\n";
    out << "      \"id\": \"" << json_escape(sf.id) << "\",\n";
    out << "      \"surf_type\": " << sf.surf_type << ",\n";
    out << "      \"rgb\": ";
    write_rgb_json(out, sf.rgb);
    out << ",\n";
    out << "      \"transparency\": " << sf.transparency << ",\n";
    out << "      \"texture_map\": \"" << json_escape(sf.texture_map) << "\"\n";
    out << "    }" << (i + 1 < smv.surfaces.size() ? "," : "") << "\n";
  }
  out << "  ],\n";

  out << "  \"vent_orig\": [\n";
  for (std::size_t i = 0; i < smv.vent_orig.size(); ++i) {
    const auto &vo = smv.vent_orig[i];
    out << "    {\n";
    out << "      \"vent_index_1based\": " << vo.vent_index_1based << ",\n";
    out << "      \"id\": \"" << json_escape(vo.id) << "\",\n";
    out << "      \"bbox\": ";
    write_bbox_json(out, vo.bbox);
    out << ",\n";
    out << "      \"raw_line\": \"" << json_escape(vo.raw_line) << "\"\n";
    out << "    }" << (i + 1 < smv.vent_orig.size() ? "," : "") << "\n";
  }
  out << "  ],\n";

  out << "  \"vents\": [\n";
  for (std::size_t i = 0; i < smv.vents.size(); ++i) {
    const auto &vt = smv.vents[i];
    out << "    {\n";
    out << "      \"mesh_index_1based\": " << vt.mesh_index_1based << ",\n";
    out << "      \"vent_index_1based\": " << vt.vent_index_1based << ",\n";
    out << "      \"circular\": " << (vt.circular ? "true" : "false") << ",\n";
    out << "      \"has_bbox\": " << (vt.has_bbox ? "true" : "false") << ",\n";
    out << "      \"bbox\": ";
    write_bbox_json(out, vt.bbox);
    out << ",\n";
    out << "      \"ordinal\": " << vt.ordinal << ",\n";
    out << "      \"surf_index\": " << vt.surf_index << ",\n";
    out << "      \"color_index\": " << vt.color_index << ",\n";
    out << "      \"type_index\": " << vt.type_index << ",\n";
    out << "      \"ior\": " << vt.ior << ",\n";
    out << "      \"has_rgb\": " << (vt.has_rgb ? "true" : "false") << ",\n";
    out << "      \"rgb\": ";
    write_rgb_json(out, vt.rgb);
    out << ",\n";
    out << "      \"transparency\": " << vt.transparency << ",\n";
    out << "      \"center\": ";
    write_rgb_json(out, vt.center);
    out << ",\n";
    out << "      \"radius\": " << vt.radius << ",\n";
    out << "      \"raw_geometry_line\": \"" << json_escape(vt.raw_geometry_line) << "\",\n";
    out << "      \"raw_display_line\": \"" << json_escape(vt.raw_display_line) << "\"\n";
    out << "    }" << (i + 1 < smv.vents.size() ? "," : "") << "\n";
  }
  out << "  ],\n";

  out << "  \"obstacles\": [\n";
  for (std::size_t i = 0; i < smv.obstacles.size(); ++i) {
    const auto &ob = smv.obstacles[i];
    out << "    {\n";
    out << "      \"mesh_index_1based\": " << ob.mesh_index_1based << ",\n";
    out << "      \"obst_index_1based\": " << ob.obst_index_1based << ",\n";
    out << "      \"raw_line\": \"" << json_escape(ob.raw_line) << "\"\n";
    out << "    }" << (i + 1 < smv.obstacles.size() ? "," : "") << "\n";
  }
  out << "  ]\n";

  out << "}\n";
}

}  // namespace bsmv
