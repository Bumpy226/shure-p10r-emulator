/* App_P10R.bin (original P10R, ATxmega256A3) -- addresses of the firmware
 * routines and variables the FIND IEMS patch uses (byte addresses). */

#define MOD_FLASH               0x29380 /* start of the unused flash */

/* menu state machine */
#define FW_SM_EVENT             0x1acf2 /* (state *, event) */
#define FW_SM_EVENT_PROLOGUE    0xb12c  /* its first instruction calls this */
#define SITE_DRAW_STATE         0x13a98 /* call FW_SM_STATE in the redraw */
#define FW_SM_STATE             0x1ae3c
#define SITE_EVENT_POLL         0x12364 /* call FW_EVENT_POLL in the main loop */
#define FW_EVENT_POLL           0x1960c
#define FW_REDRAW               0xebb0  /* what every menu action ends with */
#define RAM_REDRAW_FLAG         0x3b91
#define RAM_SM_STATE            0x2a70  /* current state (first byte) */

#define ST_MAIN_RADIO           8
#define ST_LIST_FIRST           11      /* G: CH: */
#define ST_LIST_RFPAD           20
#define ST_LIST_LAST            33      /* ANTENNA */
#define EV_ENTER                2
#define EV_EXIT                 3
#define EV_UP                   4
#define EV_DOWN                 5
#define EV_GLOBAL_1             13      /* lead to states 60 / 61 from */
#define EV_GLOBAL_2             14      /* every menu screen */

/* LCD: text is (RAM string, y, x); the frame buffer is 5 pages of 80
 * columns, bit 0 on top */
#define FW_LCD_TEXT             0x11758
#define FW_LCD_TEXT_INV         0x1177e
#define FW_RADIO_LIST_DRAW      0x14e2e /* (item 0..5) */
#define LIST_PAGE2_NO_SELECTION 6       /* items 4.. are the second page */
#define LIST_X                  10
#define RAM_FRAMEBUFFER         0x2303

/* radio */
#define FW_SCAN_STEP            0x19114 /* (dwell ms, u16 *adc, kHz) -> ok */
#define FW_SCAN_PREPARE         0x1937c
#define FW_SCAN_FINISH          0x193c0 /* back to the stored channel */
#define FW_DELAY_MS             0x1222a
#define FW_AUDIO_MUTE           0x187d0
#define FW_CLEAR_GROUP_CHANNEL  0x1e866
#define FW_SET_FREQ             0x1dc44 /* (kHz): tune and store */
#define RAM_BAND                0x3bd6  /* -> band descriptor in flash */
#define BAND_LOW                4       /* u32 kHz */
#define BAND_HIGH               8
#define RAM_TUNING_1            0x3ba7  /* set to 1 while a channel change */
#define RAM_TUNING_2            0x2a63  /* is in progress, as the frequency */
#define RAM_TUNING_3            0x2a64  /* menu does */
#define RAM_EDIT_FREQ           0x5ad9  /* the menu's copy of the frequency */
#define RAM_EDIT_GROUP          0x5add
#define RAM_EDIT_CHANNEL        0x5ade
#define IO_PILOT_PORT           0x0608  /* PORTA.IN */
#define IO_PILOT_BIT            5       /* low = pilot present */

/* RSSI detector: ADC counts at -90 dBm and counts per dB */
#define RSSI_AT_M90             634
#define RSSI_PER_DB             12

/* full scan state: sample buffer borrowed for our own state */
#define RAM_SCAN                0x3c0c
#define RAM_SCAN_BUFFER         0x3c2e  /* 6402 bytes */

/* cosmetics */
#define LOGO_BITMAP             0xa276  /* boot logo, 14 rows of 10 bytes */
#define SITE_MAIN_MENU_TITLE    0x13f0e /* call FW_MENU_DRAW with the title */
#define FW_MENU_DRAW            0x1704c /* (title, items, count, selected) */
