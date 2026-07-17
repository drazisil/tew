/*
 * hello_nt.c — minimal NT-native "Hello, LSW!" proof of concept.
 *
 * No CRT, no Win32. Calls NT syscalls directly via SYSENTER.
 * Convention: EDX = &arg1 at the time of SYSENTER (args pushed
 * last-arg-first so the first arg ends up at the top of the stack).
 *
 * Syscalls used:
 *   NtWriteFile        (0x116) — write "Hello, LSW!\n" to stdout (handle 1)
 *   NtTerminateProcess (0x103) — exit cleanly
 *
 * Syscall numbers: Windows XP SP2/SP3 x86-32.
 *
 * Build:
 *   i686-w64-mingw32-gcc -nostdlib -nostartfiles -O0 \
 *     -Wl,--entry=_NtProcessStartup hello_nt.c -o hello_nt.exe
 */

typedef struct {
    long Status;
    long Information;
} IO_STATUS_BLOCK;

void NtProcessStartup(void) {
    static const char msg[] = "Hello, LSW!\n";
    IO_STATUS_BLOCK iosb = {0, 0};

    /*
     * NtWriteFile(FileHandle, Event, ApcRoutine, ApcContext,
     *             IoStatusBlock, Buffer, Length, ByteOffset, Key)
     *
     * Push args in reverse order so arg1 (FileHandle) ends up at ESP.
     * Set EDX = ESP (= &arg1) before SYSENTER.
     */
    __asm__ volatile (
        "push $0\n\t"           /* [ESP+32] Key = NULL          (arg9) */
        "push $0\n\t"           /* [ESP+28] ByteOffset = NULL   (arg8) */
        "push $12\n\t"          /* [ESP+24] Length = 12         (arg7) */
        "push %0\n\t"           /* [ESP+20] Buffer              (arg6) */
        "push %1\n\t"           /* [ESP+16] IoStatusBlock       (arg5) */
        "push $0\n\t"           /* [ESP+12] ApcContext = NULL   (arg4) */
        "push $0\n\t"           /* [ESP+8]  ApcRoutine = NULL   (arg3) */
        "push $0\n\t"           /* [ESP+4]  Event = NULL        (arg2) */
        "push $1\n\t"           /* [ESP+0]  FileHandle = 1      (arg1) */
        "mov %%esp, %%edx\n\t"  /* EDX = &arg1 */
        "mov $0x116, %%eax\n\t"
        "sysenter\n\t"
        "add $36, %%esp\n\t"    /* pop 9 args */
        :
        : "r"(msg), "r"(&iosb)
        : "eax", "edx", "ecx", "memory"
    );

    /*
     * NtTerminateProcess(ProcessHandle, ExitStatus)
     */
    __asm__ volatile (
        "push $0\n\t"           /* [ESP+4]  ExitStatus = 0      (arg2) */
        "push $-1\n\t"          /* [ESP+0]  ProcessHandle = -1  (arg1) */
        "mov %%esp, %%edx\n\t"  /* EDX = &arg1 */
        "mov $0x103, %%eax\n\t"
        "sysenter\n\t"
        :
        :
        : "eax", "edx", "ecx", "memory"
    );

    __asm__ volatile ("ud2");   /* unreachable */
}
