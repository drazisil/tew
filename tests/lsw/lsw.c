/*
 * lsw.c — Linux Subsystem for Windows: run PE32 natively.
 *
 * Compile: gcc -m32 -O0 -o lsw lsw.c
 * Usage:   ./lsw <pe32.exe>
 *
 * How it works:
 *   1. Map PE sections at their preferred virtual addresses.
 *   2. Patch every SYSENTER (0F 34) → INT 0x2E (CD 2E) in the image.
 *      INT 0x2E from user-mode on Linux triggers #GP → SIGSEGV.
 *   3. Install SIGSEGV handler that dispatches NT syscalls directly.
 *   4. Jump to the PE entry point.
 *
 * All non-syscall x86 instructions execute on the real CPU.
 */

#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <ucontext.h>
#include <unistd.h>

/* i386 gregset_t indices (sys/ucontext.h, Linux x86-32) */
#define I386_EDI  4
#define I386_ESI  5
#define I386_EBP  6
#define I386_ESP  7
#define I386_EBX  8
#define I386_EDX  9
#define I386_ECX 10
#define I386_EAX 11
#define I386_EIP 14

/* ── PE32 structures (minimal) ───────────────────────────────────────────── */

typedef struct __attribute__((packed)) {
    uint16_t e_magic;
    uint8_t  _pad[58];
    uint32_t e_lfanew;
} DOS_HEADER;

typedef struct __attribute__((packed)) {
    uint16_t Machine;
    uint16_t NumberOfSections;
    uint32_t TimeDateStamp;
    uint32_t PointerToSymbolTable;
    uint32_t NumberOfSymbols;
    uint16_t SizeOfOptionalHeader;
    uint16_t Characteristics;
} FILE_HEADER;

typedef struct __attribute__((packed)) {
    uint16_t Magic;
    uint8_t  MajorLinkerVersion;
    uint8_t  MinorLinkerVersion;
    uint32_t SizeOfCode;
    uint32_t SizeOfInitializedData;
    uint32_t SizeOfUninitializedData;
    uint32_t AddressOfEntryPoint;   /* offset 16 */
    uint32_t BaseOfCode;
    uint32_t BaseOfData;
    uint32_t ImageBase;             /* offset 28 */
} OPT_HEADER;

typedef struct __attribute__((packed)) {
    uint32_t  Signature;
    FILE_HEADER FileHeader;
    OPT_HEADER  OptionalHeader;
} NT_HEADERS;

typedef struct __attribute__((packed)) {
    char     Name[8];
    uint32_t VirtualSize;
    uint32_t VirtualAddress;
    uint32_t SizeOfRawData;
    uint32_t PointerToRawData;
    uint32_t PointerToRelocations;
    uint32_t PointerToLinenumbers;
    uint16_t NumberOfRelocations;
    uint16_t NumberOfLinenumbers;
    uint32_t Characteristics;
} SECTION_HEADER;

#define SCN_EXEC  0x20000000
#define SCN_WRITE 0x80000000

/* ── NT syscall numbers ───────────────────────────────────────────────────── */

#define NT_WRITE_FILE        0x116
#define NT_TERMINATE_PROCESS 0x103
#define STATUS_SUCCESS       0x00000000

/* ── Helpers ─────────────────────────────────────────────────────────────── */

static uint32_t nt_arg(ucontext_t *ctx, int n) {
    uint32_t edx = (uint32_t)ctx->uc_mcontext.gregs[I386_EDX];
    return *(uint32_t *)(uintptr_t)(edx + (n - 1) * 4);
}

/* ── NT syscall dispatch (called from signal handler) ────────────────────── */

static void nt_dispatch(ucontext_t *ctx) {
    uint32_t num = (uint32_t)ctx->uc_mcontext.gregs[I386_EAX];

    switch (num) {
    case NT_WRITE_FILE: {
        uint32_t handle   = nt_arg(ctx, 1);
        uint32_t iosb_ptr = nt_arg(ctx, 5);
        uint32_t buf_ptr  = nt_arg(ctx, 6);
        uint32_t length   = nt_arg(ctx, 7);

        if (handle == 1 || handle == 2) {
            int fd = (handle == 1) ? STDOUT_FILENO : STDERR_FILENO;
            write(fd, (void *)(uintptr_t)buf_ptr, (size_t)length);
        }
        if (iosb_ptr) {
            *(uint32_t *)(uintptr_t)iosb_ptr       = STATUS_SUCCESS;
            *(uint32_t *)(uintptr_t)(iosb_ptr + 4) = length;
        }
        ctx->uc_mcontext.gregs[I386_EAX] = STATUS_SUCCESS;
        break;
    }

    case NT_TERMINATE_PROCESS: {
        uint32_t status = nt_arg(ctx, 2);
        exit((int)status);
    }

    default:
        fprintf(stderr, "lsw: unhandled NT syscall 0x%x at EIP=0x%x\n",
                num, (uint32_t)ctx->uc_mcontext.gregs[I386_EIP]);
        _exit(1);
    }
}

/* ── SIGSEGV handler ─────────────────────────────────────────────────────── */

static void on_sigsegv(int sig, siginfo_t *si, void *ucp) {
    (void)sig;
    ucontext_t *ctx = (ucontext_t *)ucp;
    uint32_t    eip = (uint32_t)ctx->uc_mcontext.gregs[I386_EIP];
    uint8_t    *pc  = (uint8_t *)(uintptr_t)eip;

    /* INT 0x2E is CD 2E; on #GP the saved EIP points at the instruction. */
    if (pc[0] == 0xCD && pc[1] == 0x2E) {
        ctx->uc_mcontext.gregs[I386_EIP] = eip + 2;
        nt_dispatch(ctx);
        return;
    }

    fprintf(stderr, "lsw: SIGSEGV at EIP=0x%08x  fault-addr=%p  opcode=%02x %02x\n",
            eip, si->si_addr, pc[0], pc[1]);
    _exit(1);
}

/* ── PE loader ───────────────────────────────────────────────────────────── */

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: lsw <pe32.exe>\n");
        return 1;
    }

    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }

    struct stat st;
    fstat(fd, &st);
    size_t fsize = (size_t)st.st_size;

    uint8_t *file = mmap(NULL, fsize, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (file == MAP_FAILED) { perror("mmap file"); return 1; }

    DOS_HEADER *dos = (DOS_HEADER *)file;
    if (dos->e_magic != 0x5A4D) {
        fprintf(stderr, "lsw: not a PE file (bad MZ magic)\n");
        return 1;
    }

    NT_HEADERS *nt = (NT_HEADERS *)(file + dos->e_lfanew);
    if (nt->Signature != 0x00004550) {
        fprintf(stderr, "lsw: bad PE signature\n");
        return 1;
    }
    if (nt->OptionalHeader.Magic != 0x010B) {
        fprintf(stderr, "lsw: not a PE32 binary\n");
        return 1;
    }

    uint32_t image_base = nt->OptionalHeader.ImageBase;
    uint32_t entry_rva  = nt->OptionalHeader.AddressOfEntryPoint;
    uint16_t nsecs      = nt->FileHeader.NumberOfSections;

    SECTION_HEADER *secs = (SECTION_HEADER *)(
        (uint8_t *)nt
        + sizeof(uint32_t)                   /* Signature */
        + sizeof(FILE_HEADER)
        + nt->FileHeader.SizeOfOptionalHeader
    );

    fprintf(stderr, "lsw: ImageBase=0x%08x  entry=0x%08x  sections=%d\n",
            image_base, image_base + entry_rva, nsecs);

    for (int i = 0; i < nsecs; i++) {
        SECTION_HEADER *s   = &secs[i];
        uint32_t va         = image_base + s->VirtualAddress;
        uint32_t vsz        = s->VirtualSize ? s->VirtualSize : s->SizeOfRawData;
        size_t   map_size   = ((size_t)vsz + 0xFFF) & ~(size_t)0xFFF;

        int prot = PROT_READ | PROT_WRITE;   /* write needed for SYSENTER patch */
        if (s->Characteristics & SCN_EXEC)  prot |= PROT_EXEC;

        void *mapped = mmap((void *)(uintptr_t)va, map_size,
                            prot, MAP_PRIVATE | MAP_FIXED | MAP_ANONYMOUS, -1, 0);
        if (mapped == MAP_FAILED) {
            fprintf(stderr, "lsw: mmap section %.8s at 0x%x failed\n",
                    s->Name, va);
            return 1;
        }

        if (s->SizeOfRawData > 0)
            memcpy(mapped, file + s->PointerToRawData,
                   s->SizeOfRawData < vsz ? s->SizeOfRawData : vsz);

        /* Patch SYSENTER (0F 34) → INT 0x2E (CD 2E) */
        uint8_t *p = (uint8_t *)mapped;
        int patches = 0;
        for (size_t j = 0; j + 1 < map_size; j++) {
            if (p[j] == 0x0F && p[j+1] == 0x34) {
                p[j]   = 0xCD;
                p[j+1] = 0x2E;
                patches++;
            }
        }
        if (patches)
            fprintf(stderr, "lsw: patched %d SYSENTER(s) in %.8s\n",
                    patches, s->Name);

        /* Remove write permission from read-only sections */
        if (!(s->Characteristics & SCN_WRITE))
            mprotect(mapped, map_size, prot & ~PROT_WRITE);

        fprintf(stderr, "lsw: mapped %.8s  va=0x%08x  size=0x%zx\n",
                s->Name, va, map_size);
    }

    munmap(file, fsize);

    /* Install SIGSEGV handler (SA_SIGINFO for ucontext access) */
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = on_sigsegv;
    sa.sa_flags     = SA_SIGINFO;
    sigaction(SIGSEGV, &sa, NULL);

    fprintf(stderr, "lsw: jumping to entry 0x%08x\n", image_base + entry_rva);

    /* Jump to PE entry point — no return expected */
    void (*entry)(void) = (void (*)(void))(uintptr_t)(image_base + entry_rva);
    entry();

    return 0; /* unreachable */
}
