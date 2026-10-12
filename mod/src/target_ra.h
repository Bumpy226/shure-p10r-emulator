/* App_P10RP_RA.bin (P10R+, Renesas RA CPU) -- addresses of the firmware
 * routines and objects the FIND IEMS patch uses.  All code addresses are
 * Thumb function entry points without the Thumb bit. */

/* unused RAM (the firmware's own data ends below 0x1FFF0300) */
#define MOD_RAM             0x1FFF8000

/* LCD helpers: (string, y, x) -> next x */
#define FW_LCD_CLEAR        0x5d7a0
#define FW_LCD_FLUSH        0x5d840
#define FW_LCD_TEXT         0x5d2a8
#define FW_LCD_TEXT_INV     0x5d322
#define FW_GET_LCD_DRIVER   0x3b89a     /* -> object holding the frame buffer */
#define LCD_FB_OFFSET       0x10        /* 5 pages of 80 columns, bit 0 on top */

/* radio */
#define FW_GET_TUNER        0x4a302     /* -> object, methods below */
#define TUNER_SET_GC        0x00        /* (this, group, channel, persist) */
#define TUNER_SET_FREQ      0x04        /* (this, kHz, persist, novalidate) */
#define TUNER_GET_FREQ      0x08
#define TUNER_GET_GC        0x0c        /* (this, u16 *group, u16 *chan) */
#define FW_BAND_RANGE       0x36e94     /* (u32 *lo, u32 *hi) kHz */
#define FW_RSSI             0x36ccc     /* (s16 *dBm) -> ok */
#define FW_GET_FPGA         0x3b882
#define FW_FPGA_SQUELCHED   0x3e26a     /* (fpga, u8 *out): 1 = no pilot */
#define FW_FPGA_PILOT_MUTE  0x3e410     /* (fpga, on) */
#define FW_AUDIO_MUTE       0x36444
#define FW_AUDIO_UNMUTE     0x36398
#define FW_DELAY_MS         0x2c6e6

/* menu base class */
#define FW_MENU_SET_TIMER   0x5c974     /* (this, n, ms) */
#define FW_MENU_SET_TICK    0x5c942     /* (this, ms) */

/* RadioMenu class */
#define RADIO_VTABLE        0x2b424
#define VT_TICK             0x08
#define VT_EXIT             0x1c
#define VT_ENTER            0x28
#define VT_UP               0x34
#define VT_DOWN             0x3c
#define VT_TIMER_1S         0x64
#define ORIG_TICK           0x615b6
#define ORIG_EXIT           0x615ca
#define ORIG_ENTER          0x61628
#define ORIG_UP             0x61962
#define ORIG_DOWN           0x61b72
#define ORIG_TIMER_1S       0x6159e
#define ORIG_DRAW           0x61dae     /* redraw current screen */
#define ORIG_LIST_DRAW      0x61e50     /* (this, selection) */
#define SITE_LIST_DRAW      0x61dea     /* bl ORIG_LIST_DRAW */
#define SITE_DRAW_FLUSH     0x61e4a     /* bl FW_LCD_FLUSH, this in r4 */

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
#define LOGO_BITMAP         0x1d4cd     /* boot logo, 14 rows of 10 bytes */
#define TITLE_MAIN_MENU     0x2a6b4     /* "MAIN MENU", 12 bytes with padding */
