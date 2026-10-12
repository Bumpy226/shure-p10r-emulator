/* FIND IEMS -- extra item in the RADIO menu of the P10R+ firmware.
 *
 * Sweeps the band, picks the narrow carriers out of the spectrum, tunes to
 * each one and keeps those where the receiver un-squelches (pilot tone
 * present, the blue RF light).  The hits are shown in a list; ENTER tunes to
 * the selected one and stays in the list.
 *
 * Built as a position-dependent blob appended to the application image; the
 * firmware is entered through the addresses in target_*.h and hooked through
 * the RadioMenu vtable (see build.py).
 */
#include TARGET_H

typedef unsigned char u8;
typedef signed char s8;
typedef unsigned short u16;
typedef short s16;
typedef unsigned int u32;
typedef int s32;

#define FN(addr, ret, ...)  ((ret (*)(__VA_ARGS__))((addr) | 1))
#define VCALL(obj, off, ret, ...) \
    ((ret (*)(__VA_ARGS__))(*(u32 *)(*(u32 *)(obj) + (off))))

#define lcd_clear       FN(FW_LCD_CLEAR, void, void)
#define lcd_flush       FN(FW_LCD_FLUSH, void, void)
#define lcd_text        FN(FW_LCD_TEXT, u32, const char *, u32, u32)
#define lcd_text_inv    FN(FW_LCD_TEXT_INV, u32, const char *, u32, u32)
#define get_lcd_driver  FN(FW_GET_LCD_DRIVER, void *, void)
#define get_tuner       FN(FW_GET_TUNER, void *, void)
#define band_range      FN(FW_BAND_RANGE, void, u32 *, u32 *)
#define read_rssi       FN(FW_RSSI, u32, s16 *)
#define get_fpga        FN(FW_GET_FPGA, void *, void)
#define fpga_squelched  FN(FW_FPGA_SQUELCHED, u32, void *, u8 *)
#define fpga_pilot_mute FN(FW_FPGA_PILOT_MUTE, void, void *, u32)
#define audio_mute      FN(FW_AUDIO_MUTE, void, void)
#define audio_unmute    FN(FW_AUDIO_UNMUTE, void, void)
#define delay_ms        FN(FW_DELAY_MS, void, u32)
#define menu_set_timer  FN(FW_MENU_SET_TIMER, void, void *, u32, u32)
#define menu_set_tick   FN(FW_MENU_SET_TICK, void, void *, u32)
#define radio_draw      FN(ORIG_DRAW, void, void *)

#define U8(p, off)      (*(u8 *)((u8 *)(p) + (off)))
#define U16(p, off)     (*(u16 *)((u8 *)(p) + (off)))
#define U32(p, off)     (*(u32 *)((u8 *)(p) + (off)))

#define SCR_LIST        0
#define SCR_SCANNING    11          /* new RadioMenu screens */
#define SCR_RESULTS     12
#define SEL_FIND        11          /* new list selection */

#define STEP_KHZ        25
#define MAX_CHANNELS    3201        /* 80 MHz, same limit as the stock scan */
#define MAX_CAND        32
#define MAX_RESULTS     32
#define DWELL_MS        5           /* as the stock full scan */
#define SWEEP_PER_TICK  12
#define PEAK_MARGIN_DB  12          /* above the noise floor */
#define PEAK_MIN_DBM    (-95)
#define MAX_WIDTH_CH    24          /* -6 dB width above 600 kHz: not an IEM */
#define GUARD_CH        8           /* carriers are at least 200 kHz apart */
#define SKIRT_RISE_DB    3           /* a rise this big is another carrier */
#define PILOT_WAIT_MS   300
#define PILOT_POLL_MS   10
#define PILOT_HITS      3
#define ROWS            3           /* result rows under the title */

enum { PH_SWEEP, PH_VERIFY };

struct result {
    u32 khz;
    s8 dbm;
};

struct state {
    u8 active;                      /* scan in progress */
    u8 phase;
    u8 ncand, nres;
    u8 vi;                          /* candidate being verified */
    u8 cursor;
    u8 forced_pilot;                /* pilot mute forced on for the scan */
    u8 tuned;                       /* result we are tuned to, 0xff none */
    u16 idx, count;
    s16 mq;                         /* marquee position */
    u16 saved_group, saved_chan;
    u32 lo, saved_khz;
    u16 cand[MAX_CAND];
    s8 cand_dbm[MAX_CAND];
    struct result res[MAX_RESULTS];
    s8 rssi[MAX_CHANNELS];
};

/* The state lives either in RAM the firmware does not use or, where there
 * is none, in the stock full scan's sample buffer (idle outside a scan; its
 * saved spectrum is read back from the serial flash when we are done). */
static struct state *state(void)
{
#ifdef MOD_RAM
    return (struct state *)MOD_RAM;
#else
    return (struct state *)((u8 *)FN(FW_GET_FULLSCAN, void *, void)() + FULLSCAN_BUFFER);
#endif
}

static void state_release(void)
{
#ifndef MOD_RAM
    FN(FW_FULLSCAN_RELOAD, void, void *)(FN(FW_GET_FULLSCAN, void *, void)());
#endif
}

/* ---- marquee ------------------------------------------------------- */

static const char marquee_text[] = "DOING THE MAGIC!";
#define MQ_LEN      16
#define MQ_SCALE    3
#define MQ_PITCH    18
#define MQ_Y        9
#define MQ_SPEED    5
#define LCD_W       80

/* 5x7 glyphs, one byte per row, bit 4 = left column */
static const u8 glyphs[][8] = {
    { 'D', 0x1e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x1e },
    { 'O', 0x0e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e },
    { 'I', 0x0e, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0e },
    { 'N', 0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11 },
    { 'G', 0x0e, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0f },
    { 'T', 0x1f, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04 },
    { 'H', 0x11, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11 },
    { 'E', 0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x1f },
    { 'M', 0x11, 0x1b, 0x15, 0x15, 0x11, 0x11, 0x11 },
    { 'A', 0x0e, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11 },
    { 'C', 0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e },
    { '!', 0x04, 0x04, 0x04, 0x04, 0x04, 0x00, 0x04 },
};

static void marquee_frame(void)
{
    struct state *S = state();
    u8 *fb = (u8 *)get_lcd_driver() + LCD_FB_OFFSET;
    s32 x, row, g;

    lcd_clear();
    for (x = 0; x < LCD_W; x++) {
        s32 sx = x + S->mq - LCD_W;         /* column of the text strip */
        s32 col;
        u8 ch;

        if (sx < 0 || sx >= MQ_LEN * MQ_PITCH)
            continue;
        col = sx % MQ_PITCH / MQ_SCALE;
        if (col >= 5)
            continue;                       /* gap between characters */
        ch = marquee_text[sx / MQ_PITCH];
        for (g = 0; g < (s32)(sizeof glyphs / sizeof glyphs[0]); g++)
            if (glyphs[g][0] == ch)
                break;
        if (g == (s32)(sizeof glyphs / sizeof glyphs[0]))
            continue;                       /* space */
        for (row = 0; row < 7; row++) {
            s32 y = MQ_Y + row * MQ_SCALE, k;
            if (!(glyphs[g][1 + row] & (0x10 >> col)))
                continue;
            for (k = y; k < y + MQ_SCALE; k++)
                fb[(k >> 3) * LCD_W + x] |= 1 << (k & 7);
        }
    }
    lcd_flush();
    S->mq += MQ_SPEED;
    if (S->mq >= LCD_W + MQ_LEN * MQ_PITCH)
        S->mq = 0;
}

/* ---- radio ----------------------------------------------------------- */

static u32 tune(u32 khz)        /* no EEPROM write; 0 if not a legal channel */
{
    void *t = get_tuner();
    return VCALL(t, TUNER_SET_FREQ, u32, void *, u32, u32, u32)(t, khz, 0, 0);
}

static s32 level(void)
{
    s16 v = -128;
    if (!read_rssi(&v) || v < -127)
        return -127;
    return v > 127 ? 127 : v;
}

static u32 pilot_present(void)
{
    u8 squelched = 1;
    if (!fpga_squelched(get_fpga(), &squelched))
        return 0;
    return !squelched;
}

static void refresh_radio_fields(void *rm)
{
    void *t = get_tuner();
    VCALL(t, TUNER_GET_GC, void, void *, u16 *, u16 *)(
        t, &U16(rm, RM_GROUP), &U16(rm, RM_CHAN));
    U32(rm, RM_FREQ) = VCALL(t, TUNER_GET_FREQ, u32, void *)(t);
}

static void scan_end(void *rm, u32 restore)
{
    struct state *S = state();
    if (restore) {
        /* as the stock scan: back to the group/channel if there was one */
        void *t = get_tuner();
        if (S->saved_group != 0xffff && S->saved_chan != 0xffff)
            VCALL(t, TUNER_SET_GC, u32, void *, u32, u32, u32)(
                t, S->saved_group, S->saved_chan, 0);
        else
            tune(S->saved_khz);
        refresh_radio_fields(rm);
    }
    if (S->forced_pilot)
        fpga_pilot_mute(get_fpga(), 0);
    audio_unmute();
    S->active = 0;
    menu_set_tick(rm, 25);
}

static void scan_start(void *rm)
{
    struct state *S = state();
    void *t = get_tuner();
    u32 lo, hi, n;

    band_range(&lo, &hi);
    n = (hi - lo) / STEP_KHZ + 1;
    S->lo = lo;
    S->count = n > MAX_CHANNELS ? MAX_CHANNELS : n;
    S->idx = 0;
    S->ncand = S->nres = 0;
    S->vi = 0;
    S->cursor = 0;
    S->tuned = 0xff;
    S->mq = 0;
    S->phase = PH_SWEEP;
    S->saved_khz = VCALL(t, TUNER_GET_FREQ, u32, void *)(t);
    VCALL(t, TUNER_GET_GC, void, void *, u16 *, u16 *)(
        t, &S->saved_group, &S->saved_chan);
    audio_mute();
    /* in NO SQUELCH mode the squelch flag says nothing about the pilot */
    S->forced_pilot = U8(rm, RM_SQUELCH) == 5;
    if (S->forced_pilot)
        fpga_pilot_mute(get_fpga(), 1);
    S->active = 1;
    U8(rm, RM_SCREEN) = SCR_SCANNING;
}

/* Pick the carriers out of the sweep: strongest first, each one centred
 * between its -6 dB points; anything wider than an FM carrier is skipped. */
static void find_candidates(void)
{
    struct state *S = state();
    s8 *r = S->rssi;
    s32 n = S->count, i, floor_dbm, thr, acc;
    u16 hist[256];

    for (i = 0; i < 256; i++)
        hist[i] = 0;
    for (i = 0; i < n; i++)
        hist[(u8)(r[i] + 128)]++;
    acc = 0;
    for (i = 1; i < 256; i++) {         /* lower quartile, bin 0 = no data */
        acc += hist[i];
        if (acc >= (n - hist[0]) / 4)
            break;
    }
    floor_dbm = i - 128;
    thr = floor_dbm + PEAK_MARGIN_DB;
    if (thr < PEAK_MIN_DBM)
        thr = PEAK_MIN_DBM;

    while (S->ncand < MAX_CAND) {
        s32 best = -128, p = -1, l, h, c, k;
        for (i = 0; i < n; i++)
            if (r[i] > best) {
                best = r[i];
                p = i;
            }
        if (p < 0 || best < thr)
            break;
        for (l = p; l > 0 && r[l - 1] >= best - 6; l--)
            ;
        for (h = p; h < n - 1 && r[h + 1] >= best - 6; h++)
            ;
        c = (l + h) / 2;
        if (h - l + 1 <= MAX_WIDTH_CH) {
            /* keep the list sorted by frequency */
            for (k = S->ncand; k > 0 && S->cand[k - 1] > c; k--) {
                S->cand[k] = S->cand[k - 1];
                S->cand_dbm[k] = S->cand_dbm[k - 1];
            }
            S->cand[k] = c;
            S->cand_dbm[k] = best;
            S->ncand++;
        }
        /* blank the carrier with its skirts: outwards for as long as the
         * level keeps falling, so a neighbouring carrier survives */
        for (k = r[l]; l > 0 && r[l - 1] >= thr && r[l - 1] <= k + SKIRT_RISE_DB; l--)
            if (r[l - 1] < k)
                k = r[l - 1];
        for (k = r[h]; h < n - 1 && r[h + 1] >= thr && r[h + 1] <= k + SKIRT_RISE_DB; h++)
            if (r[h + 1] < k)
                k = r[h + 1];
        if (l > c - GUARD_CH)
            l = c - GUARD_CH;
        if (h < c + GUARD_CH)
            h = c + GUARD_CH;
        for (i = l < 0 ? 0 : l; i <= h && i < n; i++)
            r[i] = -128;
    }
}

/* Tune to a candidate (and its neighbours, the centre estimate can be one
 * step off) and wait for the receiver to un-squelch. */
static void verify_candidate(void)
{
    struct state *S = state();
    static const s8 offs[] = { 0, 1, -1 };
    s32 c = S->cand[S->vi], o, t, hits;

    for (o = 0; o < (s32)sizeof offs; o++) {
        s32 ch = c + offs[o];
        u32 khz;
        if (ch < 0 || ch >= S->count)
            continue;
        khz = S->lo + ch * STEP_KHZ;
        if (!tune(khz))
            continue;
        hits = 0;
        for (t = 0; t < PILOT_WAIT_MS; t += PILOT_POLL_MS) {
            delay_ms(PILOT_POLL_MS);
            hits = pilot_present() ? hits + 1 : 0;
            if (hits >= PILOT_HITS)
                break;
            if (t % 40 == 30)
                marquee_frame();
        }
        if (hits >= PILOT_HITS) {
            if (S->nres < MAX_RESULTS) {
                S->res[S->nres].khz = khz;
                S->res[S->nres].dbm = level();
                S->nres++;
            }
            return;
        }
        /* no carrier left here at all: do not bother with the neighbours */
        if (level() < S->cand_dbm[S->vi] - 20)
            return;
    }
}

static void scan_tick(void *rm)
{
    struct state *S = state();
    s32 k;

    if (S->phase == PH_SWEEP) {
        for (k = 0; k < SWEEP_PER_TICK && S->idx < S->count; k++, S->idx++) {
            if (tune(S->lo + S->idx * STEP_KHZ)) {
                delay_ms(DWELL_MS);
                S->rssi[S->idx] = level();
            } else {
                S->rssi[S->idx] = -128;
            }
        }
        if (S->idx >= S->count) {
            find_candidates();
            S->phase = PH_VERIFY;
        }
    } else if (S->vi < S->ncand) {
        verify_candidate();
        S->vi++;
    } else {
        scan_end(rm, 1);
        U8(rm, RM_SCREEN) = SCR_RESULTS;
        radio_draw(rm);
        return;
    }
    marquee_frame();
    menu_set_tick(rm, 1);           /* come straight back */
}

/* ---- results list ---------------------------------------------------- */

static char *put_uint(char *p, u32 v, s32 digits)   /* digits 0: as needed */
{
    char tmp[10];
    s32 n = 0;
    do {
        tmp[n++] = '0' + v % 10;
        v /= 10;
    } while (v || n < digits);
    while (n)
        *p++ = tmp[--n];
    return p;
}

static void results_draw(void)
{
    struct state *S = state();
    char line[20], *p;
    s32 top, i;

    p = line;
    *p++ = 'R'; *p++ = 'E'; *p++ = 'S'; *p++ = 'U'; *p++ = 'L'; *p++ = 'T'; *p++ = 'S';
    if (S->nres) {
        *p++ = ' ';
        p = put_uint(p, S->cursor + 1, 0);
        *p++ = '/';
        p = put_uint(p, S->nres, 0);
    }
    *p = 0;
    lcd_text(line, 0, 0);
    if (!S->nres) {
        lcd_text("NO IEMS FOUND", 15, 0);
        return;
    }
    top = S->cursor / ROWS * ROWS;
    for (i = top; i < top + ROWS && i < S->nres; i++) {
        s32 dbm = S->res[i].dbm;
        p = put_uint(line, i + 1, 0);
        *p++ = '.';
        p = put_uint(p, S->res[i].khz / 1000, 0);
        *p++ = '.';
        p = put_uint(p, S->res[i].khz % 1000, 3);
        *p++ = i == S->tuned ? '*' : ' ';
        if (dbm < 0) {
            *p++ = '-';
            dbm = -dbm;
        }
        p = put_uint(p, dbm, 0);
        *p = 0;
        (i == S->cursor ? lcd_text_inv : lcd_text)(line, 10 + (i - top) * 10, 0);
    }
}

static void results_connect(void *rm)
{
    struct state *S = state();
    void *t = get_tuner();

    if (!S->nres || S->cursor >= MAX_RESULTS)
        return;
    audio_mute();
    VCALL(t, TUNER_SET_FREQ, u32, void *, u32, u32, u32)(
        t, S->res[S->cursor].khz, 1, 0);
    audio_unmute();
    S->tuned = S->cursor;
    refresh_radio_fields(rm);
}

/* ---- hooks ------------------------------------------------------------- */

static u32 has_find(void *rm)
{
    return U8(rm, RM_DIVERSITY) == 1;
}

void hook_tick(void *rm, u32 timer)
{
    u32 scr = U8(rm, RM_SCREEN);

    if (scr == SCR_SCANNING || scr == SCR_RESULTS)
        menu_set_timer(rm, 2, 10000);       /* hold off the idle timeout */
    FN(ORIG_TICK, void, void *, u32)(rm, timer);
    if (scr == SCR_SCANNING && (timer & 0xff) == 0)
        scan_tick(rm);
}

void hook_timer_1s(void *rm)
{
    u32 scr = U8(rm, RM_SCREEN);
    if (scr == SCR_SCANNING || scr == SCR_RESULTS)
        return;
    FN(ORIG_TIMER_1S, void, void *)(rm);
}

void hook_enter(void *rm)
{
    u32 scr = U8(rm, RM_SCREEN);

    if (scr == SCR_LIST && U8(rm, RM_SEL) == SEL_FIND) {
        scan_start(rm);
        marquee_frame();
        menu_set_tick(rm, 1);
    } else if (scr == SCR_RESULTS) {
        results_connect(rm);
        radio_draw(rm);
    } else if (scr != SCR_SCANNING) {
        FN(ORIG_ENTER, void, void *)(rm);
    }
}

void hook_exit(void *rm)
{
    u32 scr = U8(rm, RM_SCREEN);

    if (scr == SCR_SCANNING) {
        scan_end(rm, 1);
        state_release();
    } else if (scr == SCR_RESULTS) {
        state_release();
    } else {
        FN(ORIG_EXIT, void, void *)(rm);
        return;
    }
    U8(rm, RM_SCREEN) = SCR_LIST;
    radio_draw(rm);
}

static void move(void *rm, s32 dir)
{
    struct state *S = state();
    u32 scr = U8(rm, RM_SCREEN), sel = U8(rm, RM_SEL);

    if (scr == SCR_SCANNING)
        return;
    if (scr == SCR_RESULTS) {
        if (S->nres)
            S->cursor = (S->cursor + S->nres + dir) % S->nres;
    } else if (scr == SCR_LIST && has_find(rm) && sel == SEL_FIND) {
        U8(rm, RM_SEL) = dir > 0 ? SEL_FIRST : SEL_LAST;
    } else if (scr == SCR_LIST && has_find(rm) &&
               sel == (dir > 0 ? SEL_LAST : SEL_FIRST)) {
        U8(rm, RM_SEL) = SEL_FIND;
    } else {
        FN(dir > 0 ? ORIG_DOWN : ORIG_UP, void, void *)(rm);
        return;
    }
    radio_draw(rm);
}

void hook_up(void *rm)
{
    move(rm, -1);
}

void hook_down(void *rm)
{
    move(rm, 1);
}

/* replaces the call that draws the item list */
void hook_list_draw(void *rm, u32 sel)
{
    FN(ORIG_LIST_DRAW, void, void *, u32)(rm, sel);
    if (has_find(rm) && sel >= SEL_PAGE2)
        (sel == SEL_FIND ? lcd_text_inv : lcd_text)("FIND IEMS", 20, 10);
}

/* replaces the flush at the end of the screen redraw */
void draw_tail(void *rm)
{
    if (U8(rm, RM_SCREEN) == SCR_RESULTS)
        results_draw();
    lcd_flush();
}

__attribute__((naked)) void hook_draw_flush(void)
{
    __asm__("mov r0, r4\n"
            "b draw_tail\n");
}
