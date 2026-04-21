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
        if (subkey.rfind("SMOKF3D", 0) == 0 || subkey.rfind("GRID", 0) == 0) {
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
