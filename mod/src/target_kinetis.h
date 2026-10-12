/* App_P10RP.bin (P10R+, Kinetis CPU) -- addresses of the firmware routines
 * and objects the FIND IEMS patch uses.  All code addresses are Thumb
 * function entry points without the Thumb bit. */

/* no spare RAM on this CPU: borrow the full scan's sample buffer */
#define FW_GET_FULLSCAN     0x4e398     /* -> FullScanController */
#define FULLSCAN_BUFFER     0x1c        /* 6402 bytes */
#define FW_FULLSCAN_RELOAD  0x5173e     /* (this): saved spectrum from flash */

/* LCD helpers: (string, y, x) -> next x */
#define FW_LCD_CLEAR        0x45544
#define FW_LCD_FLUSH        0x455da
#define FW_LCD_TEXT         0x45058
#define FW_LCD_TEXT_INV     0x450ce
#define FW_GET_LCD_DRIVER   0x1f896     /* -> object holding the frame buffer */
#define LCD_FB_OFFSET       0x10        /* 5 pages of 80 columns, bit 0 on top */

/* radio */
#define FW_GET_TUNER        0x2236e     /* -> object, methods below */
#define TUNER_SET_GC        0x00        /* (this, group, channel, persist) */
#define TUNER_SET_FREQ      0x04        /* (this, kHz, persist, novalidate) */
#define TUNER_GET_FREQ      0x08
#define TUNER_GET_GC        0x0c        /* (this, u16 *group, u16 *chan) */
#define FW_BAND_RANGE       0x1d258     /* (u32 *lo, u32 *hi) kHz */
#define FW_RSSI             0x1d0aa     /* (s16 *dBm) -> ok */
#define FW_GET_FPGA         0x1f87e
#define FW_FPGA_SQUELCHED   0x22e64     /* (fpga, u8 *out): 1 = no pilot */
#define FW_FPGA_PILOT_MUTE  0x22ff8     /* (fpga, on) */
#define FW_AUDIO_MUTE       0x1c7f0
#define FW_AUDIO_UNMUTE     0x1c728
#define FW_DELAY_MS         0x24522

/* menu base class */
#define FW_MENU_SET_TIMER   0x447aa     /* (this, n, ms) */
#define FW_MENU_SET_TICK    0x44776     /* (this, ms) */

/* RadioMenu class */
#define RADIO_VTABLE        0x5daf8
#define VT_TICK             0x08
#define VT_EXIT             0x1c
#define VT_ENTER            0x28
#define VT_UP               0x34
#define VT_DOWN             0x3c
#define VT_TIMER_1S         0x64
#define ORIG_TICK           0x48994
#define ORIG_EXIT           0x489a8
#define ORIG_ENTER          0x48a06
#define ORIG_UP             0x48d36
#define ORIG_DOWN           0x48f4c
#define ORIG_TIMER_1S       0x4897c
#define ORIG_DRAW           0x4918e     /* redraw current screen */
#define ORIG_LIST_DRAW      0x4922e     /* (this, selection) */
#define SITE_LIST_DRAW      0x491c8     /* bl ORIG_LIST_DRAW */
#define SITE_DRAW_FLUSH     0x49228     /* bl FW_LCD_FLUSH, this in r4 */

/* RadioMenu fields */
#define RM_SCREEN           0x0c
#define RM_SEL              0x0d
#define RM_GROUP            0x0e
#define RM_CHAN             0x10
#define RM_FREQ             0x14
#define RM_SQUELCH          0x24
#define RM_DIVERSITY        0x28

#define SEL_FIRST           1
#define SEL_LAST            10          /* ANTENNA, last item of page 2 */
#define SEL_PAGE2           8           /* selections >= this are on page 2 */

/* cosmetics */
#define LOGO_BITMAP         0x5d0a1     /* boot logo, 14 rows of 10 bytes */
#define TITLE_MAIN_MENU     0x67eb0     /* "MAIN MENU", 12 bytes with padding */
