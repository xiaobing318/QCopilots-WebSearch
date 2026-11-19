# Every destination is relative to the staging root. Installing on a developer
# machine must not write to its /etc or register a system service.
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  set(QCWS_DEB_BASENAME "qcopilots-websearch-${PROJECT_VERSION}")
  string(SHA256 _account_version_hash "${PROJECT_VERSION}")
  string(SUBSTRING "${_account_version_hash}" 0 24 _account_version_hash)
  set(QCWS_DEB_ACCOUNT "qcws-${_account_version_hash}")
  set(_app_root "opt/${QCWS_DEB_BASENAME}")
  set(_app_bin "${_app_root}/bin")
  set(_app_share "${_app_root}/share/qcopilots-websearch")
  set_target_properties(qcopilots-websearch PROPERTIES
    INSTALL_RPATH "$ORIGIN/../lib" INSTALL_RPATH_USE_LINK_PATH FALSE)
  install(TARGETS qcopilots-websearch RUNTIME DESTINATION "${_app_bin}")
  qt6_generate_deploy_script(TARGET qcopilots-websearch OUTPUT_SCRIPT _deploy_script CONTENT "
    set(QT_DEPLOY_LIB_DIR \"${_app_root}/lib\")
    qt6_deploy_runtime_dependencies(
      EXECUTABLE \"$<TARGET_FILE:qcopilots-websearch>\"
      BIN_DIR \"${_app_bin}\" LIB_DIR \"${_app_root}/lib\"
      LIBEXEC_DIR \"${_app_root}/libexec\" PLUGINS_DIR \"${_app_root}/plugins\"
      NO_TRANSLATIONS NO_COMPILER_RUNTIME
      EXCLUDE_PLUGINS qsqlibase qsqlmimer qsqlmysql qsqloci qsqlodbc qsqlpsql)")
  install(SCRIPT "${_deploy_script}")
  file(MAKE_DIRECTORY "${PROJECT_BINARY_DIR}/debian")
  file(WRITE "${PROJECT_BINARY_DIR}/debian/qt.conf"
    "[Paths]\nPrefix=..\nLibraries=lib\nPlugins=plugins\nBinaries=bin\nLibraryExecutables=libexec\n")
  install(FILES "${PROJECT_BINARY_DIR}/debian/qt.conf" DESTINATION "${_app_bin}")
  configure_file(deploy/systemd/qcopilots-websearch.service
    "${PROJECT_BINARY_DIR}/debian/${QCWS_DEB_BASENAME}.service" @ONLY NEWLINE_STYLE UNIX)
  install(FILES "${PROJECT_BINARY_DIR}/debian/${QCWS_DEB_BASENAME}.service"
    DESTINATION "usr/lib/systemd/system")
else()
  set(_app_share "share/qcopilots-websearch")
  install(TARGETS qcopilots-websearch RUNTIME DESTINATION bin)
  qt_generate_deploy_app_script(TARGET qcopilots-websearch OUTPUT_SCRIPT _deploy_script
    NO_TRANSLATIONS NO_COMPILER_RUNTIME
    EXCLUDE_PLUGINS qopensslbackend qsqlibase qsqlmimer qsqlmysql qsqloci qsqlodbc qsqlpsql)
  install(SCRIPT "${_deploy_script}")
endif()
install(DIRECTORY contracts/ DESTINATION "${_app_share}/contracts")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${PROJECT_SOURCE_DIR}/config/runtime.json")
file(READ "${PROJECT_SOURCE_DIR}/config/runtime.json" _runtime)
string(JSON _runtime SET "${_runtime}" project_version "\"${PROJECT_VERSION}\"")
file(MAKE_DIRECTORY "${PROJECT_BINARY_DIR}/package-config")
file(WRITE "${PROJECT_BINARY_DIR}/package-config/runtime.json" "${_runtime}\n")
install(FILES "${PROJECT_BINARY_DIR}/package-config/runtime.json" config/runtime.schema.json config/settings.json
  config/settings.schema.json config/secrets.schema.json DESTINATION "${_app_share}/config")
install(FILES LICENSE README.md README.zh-CN.md DESTINATION "${_app_share}")
install(DIRECTORY docs/ DESTINATION "${_app_share}/docs" FILES_MATCHING PATTERN "*.md")
include("${PROJECT_SOURCE_DIR}/cmake/Licenses.cmake")
