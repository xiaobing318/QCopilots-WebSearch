option(QCWS_BUILD_WEB "Build the settings web UI using the pinned Node toolchain" ON)
set(QCWS_WEB_DIST_DIR "" CACHE PATH "Prebuilt web UI (used when QCWS_BUILD_WEB is OFF)")
set(QCWS_NPM "" CACHE FILEPATH "Pinned npm executable")
set(_web_files index.html assets/app.js assets/app.css assets/icon.svg THIRD_PARTY_NOTICES.txt)
if(QCWS_BUILD_WEB)
  if(NOT QCWS_NPM)
    find_program(_web_npm NAMES npm.cmd npm REQUIRED)
    set(QCWS_NPM "${_web_npm}")
  endif()
  get_filename_component(_npm_directory "${QCWS_NPM}" DIRECTORY)
  if(WIN32)
    set(_npm_command "${_npm_directory}/node.exe"
      "${_npm_directory}/node_modules/npm/bin/npm-cli.js")
  else()
    set(_npm_command "${QCWS_NPM}")
  endif()
  set(QCWS_NPM_COMMAND "${CMAKE_COMMAND}" -E env --modify
    "PATH=path_list_prepend:${_npm_directory}" ${_npm_command})
  set(_web_dist "${PROJECT_BINARY_DIR}/web-dist")
  file(GLOB_RECURSE _web_inputs CONFIGURE_DEPENDS
    "${PROJECT_SOURCE_DIR}/web/src/*" "${PROJECT_SOURCE_DIR}/web/public/*")
  file(GLOB _web_configs CONFIGURE_DEPENDS "${PROJECT_SOURCE_DIR}/web/*.json"
    "${PROJECT_SOURCE_DIR}/web/*.ts" "${PROJECT_SOURCE_DIR}/web/*.mjs"
    "${PROJECT_SOURCE_DIR}/web/index.html")
  set(_web_outputs ${_web_files})
  list(TRANSFORM _web_outputs PREPEND "${_web_dist}/")
  add_custom_command(OUTPUT ${_web_outputs}
    COMMAND ${QCWS_NPM_COMMAND} ci --ignore-scripts
    COMMAND ${QCWS_NPM_COMMAND} run build -- --outDir "${_web_dist}" --emptyOutDir
    COMMAND "${CMAKE_COMMAND}" "-DDIST=${_web_dist}" -P "${CMAKE_CURRENT_LIST_DIR}/CheckWebDist.cmake"
    WORKING_DIRECTORY "${PROJECT_SOURCE_DIR}/web"
    DEPENDS ${_web_inputs} ${_web_configs}
    VERBATIM)
  set_source_files_properties(${_web_outputs} PROPERTIES GENERATED TRUE)
else()
  set(_web_dist "${QCWS_WEB_DIST_DIR}")
  set(DIST "${_web_dist}")
  include("${CMAKE_CURRENT_LIST_DIR}/CheckWebDist.cmake")
endif()
set(QCWS_WEB_FILES)
foreach(_file IN LISTS _web_files)
  set(_asset "${_web_dist}/${_file}")
  set_source_files_properties("${_asset}" PROPERTIES QT_RESOURCE_ALIAS "${_file}")
  list(APPEND QCWS_WEB_FILES "${_asset}")
endforeach()
qt_add_resources(qcws_core webui PREFIX /web FILES ${QCWS_WEB_FILES})
