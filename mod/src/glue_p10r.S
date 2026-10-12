; FIND IEMS for App_P10R.bin -- glue between the firmware (IAR calling
; convention: arguments and results in r16.., data stack on Y, r4-r15 and
; r24-r27 preserved) and the patch (avr-gcc: arguments in r25..r8, r1 = 0,
; r2-r17 preserved).  The patch is built with Y reserved, so the firmware's
; data stack pointer is always valid.

#include "target_p10r.h"

    .text

; ---- firmware -> patch --------------------------------------------------

#define ENTER   push r24 $ push r25 $ push r26 $ push r27 $ clr r1
#define LEAVE   pop r27 $ pop r26 $ pop r25 $ pop r24

; replaces the first instruction of the state machine's event function
; (state *r17:r16, event r18)
    .global hook_sm_event
hook_sm_event:
    push    r18
    push    r17
    push    r16
    ENTER
    movw    r24, r16
    mov     r22, r18
    call    c_sm_event
    mov     r0, r24
    LEAVE
    pop     r16
    pop     r17
    pop     r18
    inc     r0                      ; 0xff: not ours
    breq    1f
    dec     r0
    mov     r16, r0
    ret
1:  call    FW_SM_EVENT_PROLOGUE    ; the instruction we replaced
    jmp     FW_SM_EVENT + 4

; replaces "read the current state" in the screen redraw: returns the state
    .global hook_draw_state
hook_draw_state:
    ENTER
    movw    r24, r16
    call    c_draw
    mov     r16, r24
    LEAVE
    ret

; replaces the call to the event poll in the main loop
    .global hook_poll
hook_poll:
    ENTER
    call    c_poll
    LEAVE
    jmp     FW_EVENT_POLL

; ---- patch -> firmware ----------------------------------------------------

; r2, r3, r16 and r17 are scratch for the firmware but not for gcc
#define FWCALL(name)    .global name $ name: $ push r2 $ push r3 $ push r16 $ push r17
#define FWRET(addr)     call addr $ mov r24, r16 $ pop r17 $ pop r16 $ pop r3 $ pop r2 $ clr r1 $ ret

    FWCALL(fw_text)
    movw    r16, r24
    mov     r18, r22
    mov     r19, r20
    FWRET(FW_LCD_TEXT)

    FWCALL(fw_text_inv)
    movw    r16, r24
    mov     r18, r22
    mov     r19, r20
    FWRET(FW_LCD_TEXT_INV)

    FWCALL(fw_list_draw)
    mov     r16, r24
    FWRET(FW_RADIO_LIST_DRAW)

    FWCALL(fw_scan_step)
    movw    r16, r24
    movw    r30, r22
    movw    r22, r20
    movw    r20, r18
    movw    r18, r30
    FWRET(FW_SCAN_STEP)

    FWCALL(fw_delay_ms)
    movw    r16, r22
    movw    r18, r24
    FWRET(FW_DELAY_MS)

    FWCALL(fw_set_freq)
    movw    r16, r22
    movw    r18, r24
    FWRET(FW_SET_FREQ)

    FWCALL(fw_scan_prepare)
    FWRET(FW_SCAN_PREPARE)

    FWCALL(fw_scan_finish)
    FWRET(FW_SCAN_FINISH)

    FWCALL(fw_audio_mute)
    FWRET(FW_AUDIO_MUTE)

    FWCALL(fw_clear_group_channel)
    FWRET(FW_CLEAR_GROUP_CHANNEL)

    FWCALL(fw_redraw)
    FWRET(FW_REDRAW)

; (const __memx char *s, u8 y, u8 x, u8 inverted): text from a string in
; flash; the firmware wants it in RAM, so copy it to its data stack first
    .global fw_text_far
fw_text_far:
    push    r2
    push    r3
    push    r16
    push    r17
    in      r0, 0x3b
    push    r0
    out     0x3b, r24
    movw    r30, r22
    mov     r2, r16
    sbiw    r28, 16
    movw    r26, r28
1:  elpm    r0, Z+
    st      X+, r0
    tst     r0
    brne    1b
    movw    r16, r28
    mov     r19, r18
    mov     r18, r20
    tst     r2
    brne    2f
    call    FW_LCD_TEXT
    rjmp    3f
2:  call    FW_LCD_TEXT_INV
3:  adiw    r28, 16
    pop     r0
    out     0x3b, r0
    pop     r17
    pop     r16
    pop     r3
    pop     r2
    clr     r1
    ret

; replaces the call that draws the main menu: same arguments, but the title
; (r17:r16, a RAM string) comes from flash, copied to the data stack
    .global hook_main_menu
hook_main_menu:
    in      r0, 0x3b
    push    r0
    push    r26
    push    r27
    ldi     r30, lo8(main_menu_title)
    ldi     r31, hi8(main_menu_title)
    ldi     r16, hh8(main_menu_title)
    out     0x3b, r16
    sbiw    r28, 12
    movw    r26, r28
1:  elpm    r0, Z+
    st      X+, r0
    tst     r0
    brne    1b
    pop     r27
    pop     r26
    movw    r16, r28
    call    FW_MENU_DRAW
    adiw    r28, 12
    pop     r0
    out     0x3b, r0
    ret

main_menu_title:
    .asciz  "MOD LOADED"
    .balign 2
