// Observe GLX and EGL swaps after successful submission. No GL headers needed.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static int trace = -1;
static pthread_once_t initialized = PTHREAD_ONCE_INIT;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static _Thread_local int in_swap;

static void initialize(void) {
    const char *target = getenv("BENCHER_FRAME_TARGET");
    const char *dir = getenv("BENCHER_FRAME_DIR");
    if (!target || !dir) return;
    char command[4096] = {0};
    const int fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return;
    ssize_t length = read(fd, command, sizeof(command) - 1);
    close(fd);
    if (length <= 0) return;
    const char *name = strrchr(command, '/');
    if (strcasecmp(name ? name + 1 : command, target)) return;
    char path[4096];
    int n = snprintf(path, sizeof(path), "%s/gl-%ld.bin", dir, (long)getpid());
    if (n > 0 && (size_t)n < sizeof(path))
        trace = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
}

static void record_frame(void) {
    pthread_once(&initialized, initialize);
    if (trace < 0) return;
    pthread_mutex_lock(&lock);
    struct timespec now;
    if (!clock_gettime(CLOCK_MONOTONIC, &now)) {
        const struct { uint64_t time; double reserved; } record = {
            (uint64_t) now.tv_sec * UINT64_C(1000000000) + now.tv_nsec, 0};
        (void)write(trace, &record, sizeof(record));
    }
    pthread_mutex_unlock(&lock);
}

void glXSwapBuffers(void *display, unsigned long drawable) {
    void (*next)(void *, unsigned long) = dlsym(RTLD_NEXT, "glXSwapBuffers");
    if (!next) return;
    ++in_swap;
    next(display, drawable);
    if (!--in_swap) record_frame();
}

unsigned eglSwapBuffers(void *display, void *surface) {
    unsigned (*next)(void *, void *) = dlsym(RTLD_NEXT, "eglSwapBuffers");
    if (!next) return 0;
    ++in_swap;
    unsigned result = next(display, surface);
    if (!--in_swap && result) record_frame();
    return result;
}

typedef void (*Proc)(void);

static unsigned swap_damage(const char *name, void *display, void *surface, const int *rects, int count) {
    Proc (*get)(const char *) = dlsym(RTLD_NEXT, "eglGetProcAddress");
    unsigned (*next)(void *, void *, const int *, int) = get ? (void *)get(name) : NULL;
    if (!next) return 0;
    ++in_swap;
    unsigned result = next(display, surface, rects, count);
    if (!--in_swap && result) record_frame();
    return result;
}

unsigned eglSwapBuffersWithDamageKHR(void *d, void *s, const int *rects, int count) {
    return swap_damage("eglSwapBuffersWithDamageKHR", d, s, rects, count);
}
unsigned eglSwapBuffersWithDamageEXT(void *d, void *s, const int *rects, int count) {
    return swap_damage("eglSwapBuffersWithDamageEXT", d, s, rects, count);
}

Proc eglGetProcAddress(const char *name) {
    Proc (*next)(const char *) = dlsym(RTLD_NEXT, "eglGetProcAddress");
    Proc result = next ? next(name) : NULL;
    if (result && !strcmp(name, "eglSwapBuffers")) return (Proc)eglSwapBuffers;
    if (result && !strcmp(name, "eglSwapBuffersWithDamageKHR")) return (Proc)eglSwapBuffersWithDamageKHR;
    if (result && !strcmp(name, "eglSwapBuffersWithDamageEXT")) return (Proc)eglSwapBuffersWithDamageEXT;
    return result;
}

static Proc gl_address(const char *entry, const unsigned char *name) {
    Proc (*next)(const unsigned char *) = dlsym(RTLD_NEXT, entry);
    Proc result = next ? next(name) : NULL;
    return result && !strcmp((const char *)name, "glXSwapBuffers") ? (Proc)glXSwapBuffers : result;
}

Proc glXGetProcAddress(const unsigned char *name) {
    return gl_address("glXGetProcAddress", name);
}
Proc glXGetProcAddressARB(const unsigned char *name) {
    return gl_address("glXGetProcAddressARB", name);
}
