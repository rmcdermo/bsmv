cmake_minimum_required(VERSION 3.20)
project(bsmv LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(BSMV_STRICT_WARNINGS "Enable stricter warnings" ON)

add_executable(bsmv
  src/main.cpp
  src/smv_parser.cpp
  src/s3d_reader.cpp
  src/vdb_writer.cpp
)

target_include_directories(bsmv PRIVATE src)

# -----------------------------
# OpenVDB discovery
# -----------------------------
# Preferred:
#   cmake -S . -B build -DOpenVDB_DIR=/path/to/OpenVDBConfig.cmake/dir
# or
#   cmake -S . -B build -DCMAKE_PREFIX_PATH=/openvdb/prefix
#
# Fallback:
#   pkg-config openvdb

find_package(OpenVDB CONFIG QUIET)

if(OpenVDB_FOUND)
  message(STATUS "Found OpenVDB via CMake package config")
  target_link_libraries(bsmv PRIVATE OpenVDB::openvdb)
else()
  message(STATUS "OpenVDB CMake package not found, trying pkg-config")
  find_package(PkgConfig REQUIRED)
  pkg_check_modules(OPENVDB REQUIRED openvdb)

  target_include_directories(bsmv PRIVATE ${OPENVDB_INCLUDE_DIRS})
  target_link_directories(bsmv PRIVATE ${OPENVDB_LIBRARY_DIRS})
  target_link_libraries(bsmv PRIVATE ${OPENVDB_LIBRARIES})
  target_compile_options(bsmv PRIVATE ${OPENVDB_CFLAGS_OTHER})
endif()

# -----------------------------
# Warnings
# -----------------------------
if(BSMV_STRICT_WARNINGS)
  if(MSVC)
    target_compile_options(bsmv PRIVATE /W4)
  else()
    target_compile_options(bsmv PRIVATE -Wall -Wextra -Wpedantic)
  endif()
endif()

# -----------------------------
# Filesystem note
# -----------------------------
# std::filesystem is part of C++17 on modern GCC/Clang.
# No extra link flags should be needed on current RHEL 9.x or macOS toolchains.