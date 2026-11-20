set(QCWS_QT_SOURCE_DIR "" CACHE PATH "Verified matching Qt source tree")
set(QCWS_ZLIB_SOURCE_DIR "" CACHE PATH "Verified zlib source tree")
if(NOT EXISTS "${QCWS_QT_SOURCE_DIR}/LICENSES/LGPL-3.0-only.txt")
  message(FATAL_ERROR "Matching Qt source/licenses are required. Use the build pipeline to prepare them.")
endif()
install(DIRECTORY "${QCWS_QT_SOURCE_DIR}/LICENSES/" DESTINATION "${_app_share}/licenses/qt")
install(DIRECTORY "${QCWS_QT_SOURCE_DIR}/src/3rdparty/" DESTINATION "${_app_share}/licenses/qt/thirdparty"
  FILES_MATCHING PATTERN "*LICENSE*" PATTERN "*COPYING*" PATTERN "qt_attribution.json"
  PATTERN "*.c" EXCLUDE PATTERN "*.h" EXCLUDE)
set(_licensed_dependencies cares curl httplib lexbor nlohmann_json json_schema_validator)
if(WIN32)
  list(APPEND _licensed_dependencies mbedtls)
endif()
foreach(dep IN LISTS _licensed_dependencies)
  file(GLOB _licenses LIST_DIRECTORIES FALSE
    "${${dep}_SOURCE_DIR}/LICENSE*" "${${dep}_SOURCE_DIR}/COPYING*"
    "${${dep}_SOURCE_DIR}/NOTICE*" "${${dep}_SOURCE_DIR}/AUTHORS*")
  if(NOT _licenses)
    message(FATAL_ERROR "Missing license for ${dep}")
  endif()
  install(FILES ${_licenses} DESTINATION "${_app_share}/licenses/${dep}")
endforeach()
set(_httplib_patch "${PROJECT_SOURCE_DIR}/cmake/patches/httplib_input_lifetime.py")
file(SHA256 "${_httplib_patch}" _httplib_patch_sha256)
file(SHA256 "${httplib_SOURCE_DIR}/httplib.h" _httplib_header_sha256)
file(WRITE "${PROJECT_BINARY_DIR}/httplib-modifications.txt"
  "cpp-httplib 0.66.0, modified by QCopilots-WebSearch contributors.\n"
  "Upstream MIT license applies to httplib.h. Patch script: Apache-2.0.\n"
  "Changes: absolute request-input deadlines and owned-socket shutdown.\n"
  "Patch SHA-256: ${_httplib_patch_sha256}\n"
  "Modified header SHA-256: ${_httplib_header_sha256}\n")
install(FILES "${_httplib_patch}" "${httplib_SOURCE_DIR}/httplib.h"
  DESTINATION "${_app_share}/sources/httplib")
install(FILES "${PROJECT_BINARY_DIR}/httplib-modifications.txt"
  DESTINATION "${_app_share}/licenses/httplib" RENAME MODIFICATIONS.txt)
install(FILES "${QCWS_ZLIB_SOURCE_DIR}/LICENSE" DESTINATION "${_app_share}/licenses/zlib")
foreach(_dir text global mimetypes/3rdparty)
  install(FILES "${QCWS_QT_SOURCE_DIR}/src/corelib/${_dir}/qt_attribution.json"
    DESTINATION "${_app_share}/licenses/qt/corelib/${_dir}")
endforeach()
foreach(_input QCWS_QT_PSL_DATA QCWS_ICU_LICENSE)
  set(${_input} "${${_input}}" CACHE FILEPATH "Verified redistribution file")
  if(NOT EXISTS "${${_input}}")
    message(FATAL_ERROR "Missing ${_input}. Use the build pipeline.")
  endif()
endforeach()
install(FILES "${QCWS_QT_PSL_DATA}" DESTINATION "${_app_share}/sources/qt-network"
  RENAME public_suffix_list.dat)
if(UNIX)
  install(FILES "${QCWS_ICU_LICENSE}" DESTINATION "${_app_share}/licenses/icu" RENAME LICENSE)
endif()
get_target_property(_qt_core_location Qt6::Core IMPORTED_LOCATION_RELEASE)
get_filename_component(_qt_library_dir "${_qt_core_location}" DIRECTORY)
get_filename_component(_qt_prefix "${_qt_library_dir}" DIRECTORY)
install(DIRECTORY "${_qt_prefix}/sbom/" DESTINATION "${_app_share}/sbom/qt" OPTIONAL)
install(FILES "${_qt_prefix}/config_qtbase.opt" "${_qt_prefix}/config_qtbase.summary"
  DESTINATION "${_app_share}/qt-build" OPTIONAL)
install(FILES "${PROJECT_SOURCE_DIR}/NOTICE" DESTINATION "${_app_share}")
if(WIN32)
  install(DIRECTORY "${PROJECT_SOURCE_DIR}/packaging/windows/licenses/"
    DESTINATION "${_app_share}/licenses/microsoft-runtime")
endif()

install(FILES "${PROJECT_SOURCE_DIR}/packaging/licenses/strata/LICENSE"
  DESTINATION "${_app_share}/licenses/strata")
install(FILES "${_web_dist}/THIRD_PARTY_NOTICES.txt"
  DESTINATION "${_app_share}/licenses/web")
