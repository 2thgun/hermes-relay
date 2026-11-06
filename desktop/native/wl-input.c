/*
 * wl-input — minimal Wayland pointer injector for wlroots-derived compositors.
 *
 * Why this exists
 * ---------------
 * Hyprland exposes a rich IPC surface (hyprctl) for *state* and window
 * management, and it implements zwlr_virtual_pointer_unstable_v1 for input.
 * The commonly available CLI clients are not sufficient for agent control:
 *
 *   - wlrctl can only move the pointer by a relative delta, and has no button
 *     down/up requests at all, so a real drag cannot be expressed.
 *   - wlrctl's absolute positioning was historically trapped by the focused
 *     surface (hyprwm/Hyprland#4167), and absolute virtual-pointer motion has
 *     multi-monitor bugs (hyprwm/Hyprland#6749).
 *   - ydotool needs /dev/uinput, a daemon, and root/udev rules.
 *
 * hypruse (MIT) and other current Hyprland agents solve this by speaking the
 * virtual-pointer protocol directly: relative motion for the pointer, and the
 * compositor's own `movecursor` dispatcher for absolute positioning. This
 * helper does the same, plus absolute motion for compositors whose dispatcher
 * is unavailable (sway, labwc, ...). It deliberately does NOT implement
 * keyboard input — wtype already handles XKB layouts and unicode correctly.
 *
 * Protocol: wlr-virtual-pointer-unstable-v1 (MIT, see protocols/).
 *
 * Usage:
 *   wl-input --space X Y W H move X Y
 *   wl-input --space X Y W H click X Y [left|right|middle] [count]
 *   wl-input --space X Y W H scroll X Y DX DY
 *   wl-input --space X Y W H drag X1 Y1 X2 Y2 [left|right|middle] [ms]
 *
 * Exit codes: 0 ok, 2 usage, 3 protocol unavailable, 4 compositor error.
 */

#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <linux/input-event-codes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>

#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

struct state {
    struct wl_display *display;
    struct wl_registry *registry;
    struct zwlr_virtual_pointer_manager_v1 *manager;
    struct zwlr_virtual_pointer_v1 *pointer;
    int manager_version;
};

/* Global layout in logical coordinates. Values may be negative when a
 * monitor sits left of / above the primary one. */
struct space {
    int64_t x;
    int64_t y;
    int64_t width;
    int64_t height;
};

static uint32_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)((uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u);
}

static void registry_global(void *data, struct wl_registry *registry, uint32_t name,
                            const char *interface, uint32_t version) {
    struct state *st = data;
    if (strcmp(interface, zwlr_virtual_pointer_manager_v1_interface.name) == 0) {
        st->manager_version = (int)(version < 2 ? version : 2);
        st->manager = wl_registry_bind(registry, name,
                                       &zwlr_virtual_pointer_manager_v1_interface,
                                       (uint32_t)st->manager_version);
    }
}

static void registry_global_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data;
    (void)registry;
    (void)name;
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

static int die(const char *message, int code) {
    fprintf(stderr, "wl-input: %s\n", message);
    return code;
}

static int to_int(const char *text, int64_t *out) {
    char *end = NULL;
    errno = 0;
    long long value = strtoll(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0') {
        return -1;
    }
    *out = (int64_t)value;
    return 0;
}

/* Normalise a global logical coordinate into the protocol's 0..extent frame. */
static uint32_t axis_coord(int64_t value, int64_t origin, int64_t extent) {
    int64_t relative = value - origin;
    if (relative < 0) {
        relative = 0;
    }
    if (extent <= 1) {
        return 0;
    }
    if (relative > extent - 1) {
        relative = extent - 1;
    }
    return (uint32_t)relative;
}

static uint32_t axis_extent(int64_t extent) {
    if (extent <= 1) {
        return 1;
    }
    /* The protocol treats the extent as the size of the coordinate frame. */
    return (uint32_t)extent;
}

static void pointer_move_absolute(struct state *st, const struct space *sp, int64_t x, int64_t y) {
    zwlr_virtual_pointer_v1_motion_absolute(
        st->pointer, now_ms(),
        axis_coord(x, sp->x, sp->width), axis_coord(y, sp->y, sp->height),
        axis_extent(sp->width), axis_extent(sp->height));
    zwlr_virtual_pointer_v1_frame(st->pointer);
    wl_display_flush(st->display);
}

static void pointer_button(struct state *st, uint32_t button, uint32_t state) {
    zwlr_virtual_pointer_v1_button(st->pointer, now_ms(), button, state);
    zwlr_virtual_pointer_v1_frame(st->pointer);
    wl_display_flush(st->display);
}

static uint32_t button_code(const char *name) {
    if (strcmp(name, "right") == 0) {
        return BTN_RIGHT;
    }
    if (strcmp(name, "middle") == 0) {
        return BTN_MIDDLE;
    }
    return BTN_LEFT;
}

static void sleep_ms(int milliseconds) {
    if (milliseconds <= 0) {
        return;
    }
    struct timespec ts = {
        .tv_sec = milliseconds / 1000,
        .tv_nsec = (long)(milliseconds % 1000) * 1000000L,
    };
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR) {
    }
}

/* Wait for the compositor to acknowledge everything sent so far. Without this
 * the process can exit before the socket is drained, and the last click of a
 * short sequence is silently dropped. */
static int settle(struct state *st) {
    if (wl_display_roundtrip(st->display) < 0) {
        return -1;
    }
    return wl_display_flush(st->display) < 0 ? -1 : 0;
}

static void usage(void) {
    fprintf(stderr,
            "usage: wl-input --space X Y W H <command>\n"
            "  move X Y\n"
            "  click X Y [left|right|middle] [count]\n"
            "  scroll X Y DX DY\n"
            "  drag X1 Y1 X2 Y2 [left|right|middle] [duration_ms]\n");
}

int main(int argc, char **argv) {
    struct state st = {0};
    struct space sp = {0, 0, 0, 0};
    int argi = 1;

    if (argc >= 3 && strcmp(argv[argi], "--space") == 0) {
        if (argc < 7) {
            usage();
            return 2;
        }
        if (to_int(argv[argi + 1], &sp.x) != 0 || to_int(argv[argi + 2], &sp.y) != 0 ||
            to_int(argv[argi + 3], &sp.width) != 0 || to_int(argv[argi + 4], &sp.height) != 0) {
            return die("--space expects four integers", 2);
        }
        argi += 5;
    } else {
        return die("--space X Y W H is required so coordinates map to the compositor layout", 2);
    }
    if (sp.width <= 0 || sp.height <= 0) {
        return die("--space width and height must be positive", 2);
    }
    if (argi >= argc) {
        usage();
        return 2;
    }
    const char *command = argv[argi++];

    st.display = wl_display_connect(NULL);
    if (st.display == NULL) {
        return die("cannot connect to the Wayland compositor (WAYLAND_DISPLAY unset or unreachable)", 4);
    }
    st.registry = wl_display_get_registry(st.display);
    wl_registry_add_listener(st.registry, &registry_listener, &st);
    if (wl_display_roundtrip(st.display) < 0) {
        return die("registry roundtrip failed", 4);
    }
    if (st.manager == NULL) {
        return die("compositor does not advertise zwlr_virtual_pointer_manager_v1", 3);
    }
    st.pointer = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(st.manager, NULL);
    if (st.pointer == NULL) {
        return die("could not create a virtual pointer", 4);
    }
    if (wl_display_roundtrip(st.display) < 0) {
        return die("virtual pointer roundtrip failed", 4);
    }

    int64_t a, b, c, d;
    if (strcmp(command, "move") == 0) {
        if (argc - argi < 2 || to_int(argv[argi], &a) != 0 || to_int(argv[argi + 1], &b) != 0) {
            usage();
            return 2;
        }
        pointer_move_absolute(&st, &sp, a, b);
        sleep_ms(20);
        return settle(&st) == 0 ? 0 : die("flush failed", 4);
    }

    if (strcmp(command, "click") == 0) {
        if (argc - argi < 2 || to_int(argv[argi], &a) != 0 || to_int(argv[argi + 1], &b) != 0) {
            usage();
            return 2;
        }
        const char *button = (argc - argi >= 3) ? argv[argi + 2] : "left";
        int count = 1;
        if (argc - argi >= 4) {
            int64_t parsed = 1;
            if (to_int(argv[argi + 3], &parsed) != 0 || parsed < 1 || parsed > 3) {
                return die("count must be 1..3", 2);
            }
            count = (int)parsed;
        }
        pointer_move_absolute(&st, &sp, a, b);
        sleep_ms(30);
        uint32_t code = button_code(button);
        for (int i = 0; i < count; i++) {
            pointer_button(&st, code, WL_POINTER_BUTTON_STATE_PRESSED);
            sleep_ms(20);
            pointer_button(&st, code, WL_POINTER_BUTTON_STATE_RELEASED);
            if (i + 1 < count) {
                sleep_ms(60);
            }
        }
        sleep_ms(40);
        return settle(&st) == 0 ? 0 : die("flush failed", 4);
    }

    if (strcmp(command, "scroll") == 0) {
        if (argc - argi < 4 || to_int(argv[argi], &a) != 0 || to_int(argv[argi + 1], &b) != 0 ||
            to_int(argv[argi + 2], &c) != 0 || to_int(argv[argi + 3], &d) != 0) {
            usage();
            return 2;
        }
        pointer_move_absolute(&st, &sp, a, b);
        sleep_ms(20);
        zwlr_virtual_pointer_v1_axis_source(st.pointer, WL_POINTER_AXIS_SOURCE_WHEEL);
        if (d != 0) {
            zwlr_virtual_pointer_v1_axis_discrete(
                st.pointer, now_ms(), WL_POINTER_AXIS_VERTICAL_SCROLL,
                wl_fixed_from_double((double)d * 15.0), (int32_t)d);
        }
        if (c != 0) {
            zwlr_virtual_pointer_v1_axis_discrete(
                st.pointer, now_ms(), WL_POINTER_AXIS_HORIZONTAL_SCROLL,
                wl_fixed_from_double((double)c * 15.0), (int32_t)c);
        }
        zwlr_virtual_pointer_v1_frame(st.pointer);
        wl_display_flush(st.display);
        sleep_ms(40);
        return settle(&st) == 0 ? 0 : die("flush failed", 4);
    }

    if (strcmp(command, "drag") == 0) {
        int64_t x1, y1, x2, y2;
        if (argc - argi < 4 || to_int(argv[argi], &x1) != 0 || to_int(argv[argi + 1], &y1) != 0 ||
            to_int(argv[argi + 2], &x2) != 0 || to_int(argv[argi + 3], &y2) != 0) {
            usage();
            return 2;
        }
        const char *button = (argc - argi >= 5) ? argv[argi + 4] : "left";
        int duration = 400;
        if (argc - argi >= 6) {
            int64_t parsed = 400;
            if (to_int(argv[argi + 5], &parsed) != 0 || parsed < 40 || parsed > 5000) {
                return die("duration must be 40..5000 ms", 2);
            }
            duration = (int)parsed;
        }
        const int steps = 14;
        pointer_move_absolute(&st, &sp, x1, y1);
        sleep_ms(40);
        pointer_button(&st, button_code(button), WL_POINTER_BUTTON_STATE_PRESSED);
        sleep_ms(40);
        for (int i = 1; i <= steps; i++) {
            int64_t x = x1 + (x2 - x1) * i / steps;
            int64_t y = y1 + (y2 - y1) * i / steps;
            pointer_move_absolute(&st, &sp, x, y);
            sleep_ms(duration / steps);
        }
        sleep_ms(30);
        pointer_button(&st, button_code(button), WL_POINTER_BUTTON_STATE_RELEASED);
        sleep_ms(40);
        return settle(&st) == 0 ? 0 : die("flush failed", 4);
    }

    usage();
    return 2;
}
