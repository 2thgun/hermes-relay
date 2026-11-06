# Builds the optional Wayland pointer helper.
#
# The helper is deliberately small and dependency-light: libwayland-client
# only. Wayland/scanner and the wlr protocol XML are build-time requirements;
# the resulting binary needs just libwayland-client at runtime (already present
# on every Wayland desktop).
#
#   make            # build build/wl-input
#   make install    # copy to $(PREFIX)/bin (default ~/.hermes/bin)
#   make clean

CC ?= cc
PKG_CONFIG ?= pkg-config
PREFIX ?= $(HOME)/.hermes
BUILD_DIR ?= build

WAYLAND_SCANNER ?= wayland-scanner
WAYLAND_CFLAGS := $(shell $(PKG_CONFIG) --cflags wayland-client 2>/dev/null)
WAYLAND_LIBS := $(shell $(PKG_CONFIG) --libs wayland-client 2>/dev/null)

PROTOCOL_XML := protocols/wlr-virtual-pointer-unstable-v1.xml
PROTOCOL_HEADER := $(BUILD_DIR)/wlr-virtual-pointer-unstable-v1-client-protocol.h
PROTOCOL_SOURCE := $(BUILD_DIR)/wlr-virtual-pointer-unstable-v1-protocol.c

CFLAGS ?= -O2 -Wall -Wextra -Werror
CFLAGS += $(WAYLAND_CFLAGS) -I$(BUILD_DIR)

.PHONY: all install clean check-deps

all: $(BUILD_DIR)/wl-input

check-deps:
	@command -v $(WAYLAND_SCANNER) >/dev/null || { echo "missing wayland-scanner (install libwayland-bin / wayland)" >&2; exit 1; }
	@$(PKG_CONFIG) --exists wayland-client || { echo "missing libwayland-client development files" >&2; exit 1; }
	@command -v $(CC) >/dev/null || { echo "missing C compiler" >&2; exit 1; }

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(PROTOCOL_HEADER): $(PROTOCOL_XML) | $(BUILD_DIR)
	$(WAYLAND_SCANNER) client-header $< $@

$(PROTOCOL_SOURCE): $(PROTOCOL_XML) | $(BUILD_DIR)
	$(WAYLAND_SCANNER) private-code $< $@

$(BUILD_DIR)/wl-input: wl-input.c $(PROTOCOL_HEADER) $(PROTOCOL_SOURCE) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ wl-input.c $(PROTOCOL_SOURCE) $(WAYLAND_LIBS)

install: $(BUILD_DIR)/wl-input
	mkdir -p $(PREFIX)/bin
	install -m 0755 $(BUILD_DIR)/wl-input $(PREFIX)/bin/wl-input

clean:
	rm -rf $(BUILD_DIR)
