#include "smv_parser.hpp"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace bsmv {

namespace {

std::vector<std::string> read_lines(const std::filesystem::path &path) {
  std::ifstream in(path);
  if (!in) {
    throw std::runtime_error("Could not open SMV file: " + path.string());
  }
  std::vector<std::string> lines;
  std::string line;
  while (std::getline(in, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    lines.push_back(line);
  }
  return lines;
}

int next_nonempty(const std::vector<std::string> &lines, int i) {
  while (i < static_cast<int>(lines.size()) && trim(lines[i]).empty()) {
    ++i;
  }
  return i;
}

std::vector<std::string> split_ws(const std::string &s) {
  std::istringstream iss(s);
  std::vector<std::string> out;
  std::string tok;
  while (iss >> tok) out.push_back(tok);
  return out;
}

std::vector<std::string> split_ws_before_comment(const std::string &s) {
  std::string t = s;
  const auto bang = t.find('!');
  const auto pct = t.find('%');
  std::size_t cut = std::string::npos;
  if (bang != std::string::npos) cut = bang;
  if (pct != std::string::npos) cut = std::min(cut, pct);
  if (cut != std::string::npos) t = t.substr(0, cut);
  return split_ws(t);
}

std::string after_marker(const std::string &s, char marker) {
  const auto p = s.find(marker);
  if (p == std::string::npos) return {};
  return trim(s.substr(p + 1));
}

bool parse_int_token(const std::string &tok, int &value) {
  try {
    std::size_t pos = 0;
    const int v = std::stoi(tok, &pos);
    if (pos == tok.size()) {
      value = v;
      return true;
    }
  } catch (...) {
  }
  return false;
}

bool parse_double_token(const std::string &tok, double &value) {
  try {
    std::size_t pos = 0;
    const double v = std::stod(tok, &pos);
    if (pos == tok.size()) {
      value = v;
      return true;
    }
  } catch (...) {
  }
  return false;
}

int parse_optional_count_after_keyword(
    const std::vector<std::string> &lines,
    int &i,
    const std::vector<std::string> &toks) {
  int n = 0;
  if (toks.size() >= 2 && parse_int_token(toks[1], n)) return n;

  const int j = next_nonempty(lines, i + 1);
  if (j < static_cast<int>(lines.size())) {
    const auto ntoks = split_ws(lines[j]);
    if (ntoks.size() == 1 && parse_int_token(ntoks[0], n)) {
      i = j;
      return n;
    }
  }
  return 0;
}

int parse_last_int_or_zero(const std::string &s) {
  const auto toks = split_ws(s);
  for (auto it = toks.rbegin(); it != toks.rend(); ++it) {
    int v = 0;
    if (parse_int_token(*it, v)) return v;
  }
  return 0;
}

bool looks_like_ge_file(const std::string &s) {
  const std::string u = upper_copy(trim(s));
  return u.size() >= 3 &&
         (u.find(".GE") != std::string::npos ||
          u.find(".GE2") != std::string::npos);
}

bool looks_like_new_smv_keyword(const std::string &s) {
  const std::string u = upper_copy(trim(s));
  if (u.empty()) return false;

  static const char *keys[] = {
      "CHID", "GRID", "TRNX", "TRNY", "TRNZ",
      "SMOKF3D", "GEOM", "BOXGEOM", "SLCF", "BNDF",
      "BNDE", "ISOF", "PRT5", "DEVICE", "VIEWTIMES",
      "HRRPUV_MINMAX", "TEMP_MINMAX", "CLASS_OF_PARTICLES", "CSVF",
      "SURFDEF", "SURFACE", "VENTORIG", "OBST", "VENT", "CVENT",
      "IBLANK", "GVEC", "ALBEDO", "FDSVERSION", "INPF", "REVISION",
      "NMESHES", "TIMES", "OUTLINE", "TERRAINIMAGE", "CADGEOM"};
  for (const char *k : keys) {
    if (u == k || u.rfind(std::string(k) + " ", 0) == 0) return true;
  }
  return false;
}

SurfaceInfo parse_surface_block(const std::vector<std::string> &lines, int &i, int surface_index) {
  SurfaceInfo sf;
  sf.surface_index = surface_index;

  i = next_nonempty(lines, i + 1);
  if (i >= static_cast<int>(lines.size())) return sf;
  sf.id = trim(lines[i]);

  i = next_nonempty(lines, i + 1);
  if (i < static_cast<int>(lines.size())) {
    const auto toks = split_ws_before_comment(lines[i]);
    if (toks.size() >= 2) {
      parse_double_token(toks[0], sf.temperature);
      parse_double_token(toks[1], sf.emissivity);
    }
  }

  i = next_nonempty(lines, i + 1);
  if (i < static_cast<int>(lines.size())) {
    const auto toks = split_ws_before_comment(lines[i]);
    if (toks.size() >= 7) {
      parse_int_token(toks[0], sf.surf_type);
      parse_double_token(toks[1], sf.texture_width);
      parse_double_token(toks[2], sf.texture_height);
      parse_double_token(toks[3], sf.rgb[0]);
      parse_double_token(toks[4], sf.rgb[1]);
      parse_double_token(toks[5], sf.rgb[2]);
      parse_double_token(toks[6], sf.transparency);
    }
  }

  i = next_nonempty(lines, i + 1);
  if (i < static_cast<int>(lines.size())) {
    sf.texture_map = trim(lines[i]);
  }

  ++i;
  return sf;
}

VentOrigInfo parse_ventorig_line(const std::string &line, int index_1based) {
  VentOrigInfo vo;
  vo.vent_index_1based = index_1based;
  vo.raw_line = trim(line);
  vo.id = after_marker(line, '!');

  const auto toks = split_ws_before_comment(line);
  if (toks.size() >= 6) {
    for (int q = 0; q < 6; ++q) parse_double_token(toks[static_cast<std::size_t>(q)], vo.bbox[static_cast<std::size_t>(q)]);
  }
  return vo;
}

VentInfo parse_vent_pair(
    const std::string &geom_line,
    const std::string &display_line,
    int mesh_index_1based,
    int vent_index_1based,
    bool circular) {
  VentInfo vt;
  vt.mesh_index_1based = mesh_index_1based;
  vt.vent_index_1based = vent_index_1based;
  vt.circular = circular;
  vt.raw_geometry_line = trim(geom_line);
  vt.raw_display_line = trim(display_line);

  const auto gtoks = split_ws_before_comment(geom_line);
  if (gtoks.size() >= 6) {
    bool ok = true;
    for (int q = 0; q < 6; ++q) {
      if (!parse_double_token(gtoks[static_cast<std::size_t>(q)], vt.bbox[static_cast<std::size_t>(q)])) ok = false;
    }
    vt.has_bbox = ok;
  }
  if (gtoks.size() >= 8) {
    parse_int_token(gtoks[6], vt.ordinal);
    parse_int_token(gtoks[7], vt.surf_index);
  }

  // Circular vents have center/radius after a '%' marker.
  if (circular) {
    const auto pct = geom_line.find('%');
    if (pct != std::string::npos) {
      const auto ctoks = split_ws(geom_line.substr(pct + 1));
      if (ctoks.size() >= 4) {
        parse_double_token(ctoks[0], vt.center[0]);
        parse_double_token(ctoks[1], vt.center[1]);
        parse_double_token(ctoks[2], vt.center[2]);
        parse_double_token(ctoks[3], vt.radius);
      }
    }
  }

  const auto dtoks = split_ws_before_comment(display_line);
  if (dtoks.size() >= 8) {
    parse_int_token(dtoks[6], vt.color_index);
    parse_int_token(dtoks[7], vt.type_index);
  }
  if (dtoks.size() >= 12) {
    bool ok = true;
    ok = parse_double_token(dtoks[8], vt.rgb[0]) && ok;
    ok = parse_double_token(dtoks[9], vt.rgb[1]) && ok;
    ok = parse_double_token(dtoks[10], vt.rgb[2]) && ok;
    ok = parse_double_token(dtoks[11], vt.transparency) && ok;
    vt.has_rgb = ok;
  }

  const std::string ior_text = after_marker(display_line, '!');
  if (!ior_text.empty()) {
    const auto itoks = split_ws(ior_text);
    if (!itoks.empty()) parse_int_token(itoks[0], vt.ior);
  }

  return vt;
}

void parse_vent_block(
    const std::vector<std::string> &lines,
    int &i,
    int current_mesh_index,
    bool circular,
    std::vector<VentInfo> &vents) {
  const auto toks = split_ws(lines[i]);
  const int nvent = parse_optional_count_after_keyword(lines, i, toks);

  ++i;
  std::vector<std::string> geom_lines;
  geom_lines.reserve(static_cast<std::size_t>(std::max(0, nvent)));
  for (int n = 0; n < nvent; ++n) {
    i = next_nonempty(lines, i);
    if (i >= static_cast<int>(lines.size()) || looks_like_new_smv_keyword(lines[i])) break;
    geom_lines.push_back(lines[i]);
    ++i;
  }

  std::vector<std::string> display_lines;
  display_lines.reserve(geom_lines.size());
  for (std::size_t n = 0; n < geom_lines.size(); ++n) {
    i = next_nonempty(lines, i);
    if (i >= static_cast<int>(lines.size()) || looks_like_new_smv_keyword(lines[i])) break;
    display_lines.push_back(lines[i]);
    ++i;
  }

  const std::size_t npair = std::min(geom_lines.size(), display_lines.size());
  for (std::size_t n = 0; n < npair; ++n) {
    vents.push_back(parse_vent_pair(
        geom_lines[n],
        display_lines[n],
        current_mesh_index,
        static_cast<int>(n + 1),
        circular));
  }
}

void parse_obst_block(
    const std::vector<std::string> &lines,
    int &i,
    int current_mesh_index,
    std::vector<ObstInfo> &obstacles) {
  const auto toks = split_ws(lines[i]);
  const int nobst = parse_optional_count_after_keyword(lines, i, toks);
  ++i;

  for (int n = 0; n < nobst; ++n) {
    i = next_nonempty(lines, i);
    if (i >= static_cast<int>(lines.size()) || looks_like_new_smv_keyword(lines[i])) break;

    ObstInfo ob;
    ob.mesh_index_1based = current_mesh_index;
    ob.obst_index_1based = n + 1;
    ob.raw_line = trim(lines[i]);
    obstacles.push_back(std::move(ob));
    ++i;
  }
}

}  // namespace

std::string trim(const std::string &s) {
  std::size_t a = 0;
  while (a < s.size() && std::isspace(static_cast<unsigned char>(s[a]))) ++a;
  std::size_t b = s.size();
  while (b > a && std::isspace(static_cast<unsigned char>(s[b - 1]))) --b;
  return s.substr(a, b - a);
}

std::string upper_copy(const std::string &s) {
  std::string out = s;
  std::transform(out.begin(), out.end(), out.begin(),
                 [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
  return out;
}

std::string canonical_quantity(const std::string &name) {
  const std::string q = upper_copy(trim(name));
  if (q == "EFFECTIVE FLAME TEMPERATURE") return "TEMPERATURE";
  if (q == "TEMPERATURE") return "TEMPERATURE";
  if (q == "HRRPUV") return "HRRPUV";
  if (q == "DENSITY") return "DENSITY";
  if (q.size() > 8 && q.substr(q.size() - 8) == " DENSITY") return "DENSITY";
  return q;
}

SmvData parse_smv_file(const std::filesystem::path &path) {
  const auto lines = read_lines(path);
  SmvData smv;
  int i = 0;
  int current_mesh_index = 0;

  while (i < static_cast<int>(lines.size())) {
    i = next_nonempty(lines, i);
    if (i >= static_cast<int>(lines.size())) break;

    const std::string key = upper_copy(trim(lines[i]));

    if (key == "CHID") {
      i = next_nonempty(lines, i + 1);
      if (i < static_cast<int>(lines.size())) smv.chid = trim(lines[i]);
      ++i;
      continue;
    }

    if (key == "HRRPUV_MINMAX") {
      i = next_nonempty(lines, i + 1);
      if (i < static_cast<int>(lines.size())) {
        const auto toks = split_ws(lines[i]);
        if (toks.size() >= 2) {
          smv.hrrpuv_min = std::stod(toks[0]);
          smv.hrrpuv_max = std::stod(toks[1]);
        }
      }
      ++i;
      continue;
    }

    if (key == "TEMP_MINMAX") {
      i = next_nonempty(lines, i + 1);
      if (i < static_cast<int>(lines.size())) {
        const auto toks = split_ws(lines[i]);
        if (toks.size() >= 2) {
          smv.temp_min = std::stod(toks[0]);
          smv.temp_max = std::stod(toks[1]);
        }
      }
      ++i;
      continue;
    }

    if (key == "SURFDEF") {
      i = next_nonempty(lines, i + 1);
      if (i < static_cast<int>(lines.size())) smv.default_surface_id = trim(lines[i]);
      ++i;
      continue;
    }

    if (key == "SURFACE") {
      smv.surfaces.push_back(parse_surface_block(lines, i, static_cast<int>(smv.surfaces.size())));
      continue;
    }

    if (key == "VENTORIG") {
      const auto toks = split_ws(lines[i]);
      const int nvent = parse_optional_count_after_keyword(lines, i, toks);
      ++i;
      for (int n = 0; n < nvent; ++n) {
        i = next_nonempty(lines, i);
        if (i >= static_cast<int>(lines.size()) || looks_like_new_smv_keyword(lines[i])) break;
        smv.vent_orig.push_back(parse_ventorig_line(lines[i], n + 1));
        ++i;
      }
      continue;
    }

    if (key == "OBST") {
      parse_obst_block(lines, i, current_mesh_index, smv.obstacles);
      continue;
    }

    if (key == "VENT") {
      parse_vent_block(lines, i, current_mesh_index, false, smv.vents);
      continue;
    }

    if (key == "CVENT") {
      parse_vent_block(lines, i, current_mesh_index, true, smv.vents);
      continue;
    }

    if (key.rfind("BOXGEOM", 0) == 0) {
      const auto toks = split_ws(lines[i]);
      int nbox = parse_optional_count_after_keyword(lines, i, toks);
      if (nbox <= 0 && smv.geom.n_geometry > 0) nbox = smv.geom.n_geometry;

      ++i;
      for (int g = 0; g < nbox; ++g) {
        i = next_nonempty(lines, i);
        if (i >= static_cast<int>(lines.size())) break;
        if (looks_like_new_smv_keyword(lines[i])) break;

        const auto btoks = split_ws(lines[i]);
        if (btoks.size() >= 6 && g < static_cast<int>(smv.geom.entries.size())) {
          bool ok = true;
          for (int q = 0; q < 6; ++q) {
            double v = 0.0;
            if (!parse_double_token(btoks[q], v)) {
              ok = false;
              break;
            }
            smv.geom.entries[static_cast<std::size_t>(g)].bbox[static_cast<std::size_t>(q)] = v;
          }
          if (!ok) {
            // Keep going. BOXGEOM is useful but not required to read the .ge file.
          }
        }
        ++i;
      }
      continue;
    }

    if (key.rfind("GEOM", 0) == 0) {
      const auto toks = split_ws(lines[i]);
      smv.geom.n_geometry = parse_optional_count_after_keyword(lines, i, toks);
      smv.geom.entries.clear();
      smv.geom.entries.reserve(static_cast<std::size_t>(std::max(0, smv.geom.n_geometry)));

      ++i;
      i = next_nonempty(lines, i);

      if (i < static_cast<int>(lines.size()) && looks_like_ge_file(lines[i])) {
        smv.geom.ge_filename = trim(lines[i]);
        ++i;
      }

      for (int g = 1; g <= smv.geom.n_geometry; ++g) {
        i = next_nonempty(lines, i);
        if (i >= static_cast<int>(lines.size())) break;
        if (looks_like_new_smv_keyword(lines[i])) break;

        GeomSmvEntry ent;
        ent.geom_index_1based = g;
        ent.metadata_line = trim(lines[i]);
        ent.n_faces_hint = parse_last_int_or_zero(ent.metadata_line);
        smv.geom.entries.push_back(std::move(ent));
        ++i;
      }

      while (static_cast<int>(smv.geom.entries.size()) < smv.geom.n_geometry) {
        GeomSmvEntry ent;
        ent.geom_index_1based = static_cast<int>(smv.geom.entries.size()) + 1;
        smv.geom.entries.push_back(std::move(ent));
      }

      continue;
    }

    if (key.rfind("GRID", 0) == 0) {
      i = next_nonempty(lines, i + 1);
      if (i >= static_cast<int>(lines.size())) break;
      const auto toks = split_ws(lines[i]);
      if (toks.size() < 3) {
        throw std::runtime_error("Malformed GRID dimensions line in SMV.");
      }
      MeshGrid grid;
      grid.ibar = std::stoi(toks[0]);
      grid.jbar = std::stoi(toks[1]);
      grid.kbar = std::stoi(toks[2]);
      grid.mesh_index_1based = static_cast<int>(smv.grids.size()) + 1;
      current_mesh_index = grid.mesh_index_1based;
      ++i;

      bool got_x = false, got_y = false, got_z = false;
      while (i < static_cast<int>(lines.size())) {
        i = next_nonempty(lines, i);
        if (i >= static_cast<int>(lines.size())) break;
        const std::string subkey = upper_copy(trim(lines[i]));

        auto read_trn = [&](std::vector<double> &coords, int ncell) {
          i = next_nonempty(lines, i + 1);  // skip line like "0"
          if (i >= static_cast<int>(lines.size())) {
            throw std::runtime_error("Unexpected EOF while reading TRN block.");
          }
          ++i;
          coords.clear();
          coords.reserve(static_cast<std::size_t>(ncell + 1));
          for (int j = 0; j <= ncell; ++j, ++i) {
            if (i >= static_cast<int>(lines.size())) {
              throw std::runtime_error("Unexpected EOF in TRN coordinates.");
            }
            const auto xy = split_ws(lines[i]);
            if (xy.size() < 2) {
              throw std::runtime_error("Malformed TRN coordinate line.");
            }
            coords.push_back(std::stod(xy[1]));
          }
        };

        if (subkey == "TRNX") {
          read_trn(grid.x, grid.ibar);
          got_x = true;
          continue;
        }
        if (subkey == "TRNY") {
          read_trn(grid.y, grid.jbar);
          got_y = true;
          continue;
        }
        if (subkey == "TRNZ") {
          read_trn(grid.z, grid.kbar);
          got_z = true;
          break;
        }
        if (looks_like_new_smv_keyword(lines[i])) {
          break;
        }
        ++i;
      }

      if (!got_x || !got_y || !got_z) {
        throw std::runtime_error("Did not find complete TRNX/TRNY/TRNZ blocks in SMV.");
      }
      smv.grids[grid.mesh_index_1based] = std::move(grid);
      continue;
    }

    if (key.rfind("SMOKF3D", 0) == 0) {
      const auto toks = split_ws(lines[i]);
      if (toks.size() < 2) {
        throw std::runtime_error("Malformed SMOKF3D line in SMV.");
      }
      SmokeFileEntry ent;
      ent.mesh_index_1based = std::stoi(toks[1]);
      if (toks.size() >= 3) ent.value = std::stod(toks[2]);

      i = next_nonempty(lines, i + 1);
      if (i >= static_cast<int>(lines.size())) break;
      ent.filename = trim(lines[i]);

      i = next_nonempty(lines, i + 1);
      if (i >= static_cast<int>(lines.size())) break;
      ent.quantity = trim(lines[i]);

      i = next_nonempty(lines, i + 1);
      if (i >= static_cast<int>(lines.size())) break;
      ent.short_name = trim(lines[i]);

      i = next_nonempty(lines, i + 1);
      if (i >= static_cast<int>(lines.size())) break;
      ent.units = trim(lines[i]);

      smv.smoke_entries.push_back(std::move(ent));
      ++i;
      continue;
    }

    ++i;
  }

  return smv;
}

std::unordered_map<int, SmokeFileEntry> find_smokf3d_entries(
    const SmvData &smv,
    const std::string &wanted_quantity) {
  std::unordered_map<int, SmokeFileEntry> out;
  const std::string want = canonical_quantity(wanted_quantity);
  for (const auto &ent : smv.smoke_entries) {
    if (canonical_quantity(ent.quantity) == want) {
      if (out.find(ent.mesh_index_1based) == out.end()) {
        out.emplace(ent.mesh_index_1based, ent);
      }
    }
  }
  return out;
}

}  // namespace bsmv
