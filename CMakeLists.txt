cmake_minimum_required(VERSION 3.20)
project(bsmv LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(BSMV_STRICT_WARNINGS "Enable stricter compiler warnings" ON)

add_executable(bsmv
  src/main.cpp
  src/smv_parser.cpp
  src/s3d_reader.cpp
  src/vdb_writer.cpp
)

target_include_directories(bsmv PRIVATE src)

# -----------------------------------------------------------------------------
# OpenVDB / TBB discovery
#
# Discovery order:
#   1. CMake package config
#   2. pkg-config
#   3. Manual fallback (with Homebrew support on macOS)
#
# Useful overrides:
#   -DOpenVDB_DIR=/path/to/lib/cmake/OpenVDB
#   -DCMAKE_PREFIX_PATH=/prefix
#   -DOPENVDB_ROOT=/prefix
# -----------------------------------------------------------------------------

set(_bsmv_openvdb_found FALSE)

# 1) Official CMake packages first.
find_package(OpenVDB CONFIG QUIET)
find_package(TBB CONFIG QUIET)

if(OpenVDB_FOUND)
  message(STATUS "Found OpenVDB via CMake package config")
  target_link_libraries(bsmv PRIVATE OpenVDB::openvdb)

  # Some OpenVDB package configs propagate TBB, some do not.
  if(TBB_FOUND)
    message(STATUS "Found TBB via CMake package config")
    if(TARGET TBB::tbb)
      target_link_libraries(bsmv PRIVATE TBB::tbb)
    endif()
  endif()

  set(_bsmv_openvdb_found TRUE)
endif()

# 2) pkg-config fallback
if(NOT _bsmv_openvdb_found)
  find_package(PkgConfig QUIET)
  if(PkgConfig_FOUND)
    pkg_check_modules(OPENVDB_PC QUIET openvdb)
    pkg_check_modules(TBB_PC QUIET tbb)

    if(OPENVDB_PC_FOUND)
      message(STATUS "Found OpenVDB via pkg-config")
      target_include_directories(bsmv PRIVATE ${OPENVDB_PC_INCLUDE_DIRS})
      target_link_directories(bsmv PRIVATE ${OPENVDB_PC_LIBRARY_DIRS})
      target_link_libraries(bsmv PRIVATE ${OPENVDB_PC_LIBRARIES})
      target_compile_options(bsmv PRIVATE ${OPENVDB_PC_CFLAGS_OTHER})

      if(TBB_PC_FOUND)
        message(STATUS "Found TBB via pkg-config")
        target_include_directories(bsmv PRIVATE ${TBB_PC_INCLUDE_DIRS})
        target_link_directories(bsmv PRIVATE ${TBB_PC_LIBRARY_DIRS})
        target_link_libraries(bsmv PRIVATE ${TBB_PC_LIBRARIES})
        target_compile_options(bsmv PRIVATE ${TBB_PC_CFLAGS_OTHER})
      endif()

      set(_bsmv_openvdb_found TRUE)
    endif()
  endif()
endif()

# 3) Manual fallback
if(NOT _bsmv_openvdb_found)
  set(_bsmv_prefix_hints "")

  if(DEFINED OPENVDB_ROOT AND NOT OPENVDB_ROOT STREQUAL "")
    list(APPEND _bsmv_prefix_hints "${OPENVDB_ROOT}")
  endif()

  if(DEFINED ENV{OPENVDB_ROOT} AND NOT "$ENV{OPENVDB_ROOT}" STREQUAL "")
    list(APPEND _bsmv_prefix_hints "$ENV{OPENVDB_ROOT}")
  endif()

  if(APPLE)
    find_program(BREW_EXECUTABLE brew)
    if(BREW_EXECUTABLE)
      execute_process(
        COMMAND "${BREW_EXECUTABLE}" --prefix
        OUTPUT_VARIABLE _brew_prefix
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
      )
      if(_brew_prefix)
        list(APPEND _bsmv_prefix_hints "${_brew_prefix}")
      endif()

      execute_process(
        COMMAND "${BREW_EXECUTABLE}" --prefix openvdb
        OUTPUT_VARIABLE _brew_openvdb_prefix
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
      )
      if(_brew_openvdb_prefix)
        list(APPEND _bsmv_prefix_hints "${_brew_openvdb_prefix}")
      endif()

      execute_process(
        COMMAND "${BREW_EXECUTABLE}" --prefix tbb
        OUTPUT_VARIABLE _brew_tbb_prefix
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
      )
      if(_brew_tbb_prefix)
        list(APPEND _bsmv_prefix_hints "${_brew_tbb_prefix}")
      endif()
    endif()

    list(APPEND _bsmv_prefix_hints
      "/opt/homebrew"
      "/opt/homebrew/opt/openvdb"
      "/opt/homebrew/opt/tbb"
      "/usr/local"
      "/usr/local/opt/openvdb"
      "/usr/local/opt/tbb"
    )
  else()
    list(APPEND _bsmv_prefix_hints
      "/usr"
      "/usr/local"
      "/opt"
      "/opt/local"
    )
  endif()

  list(REMOVE_DUPLICATES _bsmv_prefix_hints)

  find_path(OPENVDB_INCLUDE_DIR
    NAMES openvdb/openvdb.h
    HINTS ${_bsmv_prefix_hints}
    PATH_SUFFIXES include
  )

  find_library(OPENVDB_LIBRARY
    NAMES openvdb
    HINTS ${_bsmv_prefix_hints}
    PATH_SUFFIXES lib lib64
  )

  find_path(TBB_INCLUDE_DIR
    NAMES oneapi/tbb.h tbb/tbb.h
    HINTS ${_bsmv_prefix_hints}
    PATH_SUFFIXES include
  )

  find_library(TBB_LIBRARY
    NAMES tbb
    HINTS ${_bsmv_prefix_hints}
    PATH_SUFFIXES lib lib64
  )

  if(OPENVDB_INCLUDE_DIR AND OPENVDB_LIBRARY)
    message(STATUS "Found OpenVDB via manual include/library search")
    message(STATUS "  OPENVDB_INCLUDE_DIR = ${OPENVDB_INCLUDE_DIR}")
    message(STATUS "  OPENVDB_LIBRARY     = ${OPENVDB_LIBRARY}")

    get_filename_component(_openvdb_prefix_from_include "${OPENVDB_INCLUDE_DIR}" DIRECTORY)
    get_filename_component(_openvdb_libdir "${OPENVDB_LIBRARY}" DIRECTORY)
    get_filename_component(_openvdb_prefix_from_lib "${_openvdb_libdir}" DIRECTORY)

    target_include_directories(bsmv PRIVATE
      "${OPENVDB_INCLUDE_DIR}"
      "${_openvdb_prefix_from_include}"
      "${_openvdb_prefix_from_lib}/include"
    )
    target_link_libraries(bsmv PRIVATE "${OPENVDB_LIBRARY}")

    if(TBB_INCLUDE_DIR)
      target_include_directories(bsmv PRIVATE "${TBB_INCLUDE_DIR}")
    endif()

    if(TBB_LIBRARY)
      message(STATUS "Found TBB via manual include/library search")
      message(STATUS "  TBB_INCLUDE_DIR     = ${TBB_INCLUDE_DIR}")
      message(STATUS "  TBB_LIBRARY         = ${TBB_LIBRARY}")
      target_link_libraries(bsmv PRIVATE "${TBB_LIBRARY}")
    else()
      message(WARNING "TBB library not found during manual fallback. OpenVDB may fail to link.")
    endif()

    set(_bsmv_openvdb_found TRUE)
  endif()
endif()

if(NOT _bsmv_openvdb_found)
  message(FATAL_ERROR
    "Could not find OpenVDB.\n"
    "Tried CMake package config, pkg-config, and manual prefix search.\n"
    "You can help CMake by setting one of:\n"
    "  -DOpenVDB_DIR=/path/to/lib/cmake/OpenVDB\n"
    "  -DCMAKE_PREFIX_PATH=/prefix\n"
    "  -DOPENVDB_ROOT=/prefix"
  )
endif()

if(BSMV_STRICT_WARNINGS)
  if(MSVC)
    target_compile_options(bsmv PRIVATE /W4)
  else()
    target_compile_options(bsmv PRIVATE -Wall -Wextra -Wpedantic)
  endif()
endif()