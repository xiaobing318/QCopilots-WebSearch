// SPDX-License-Identifier: Apache-2.0
#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

typedef SRWLOCK mbedtls_threading_mutex_t;
