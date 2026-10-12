/* FIND IEMS for the original P10R (ATxmega256A3) -- same feature as
 * find_iems.c, hooked into this firmware's table-driven menu state machine.
 *
 * Three new states follow the firmware's own 160: the extra item in the
 * RADIO list, the scan and the result list.  They are not in the firmware's
 * tables; glue_p10r.S intercepts the state machine's event function, the
 * screen redraw and the main loop instead.
 *
 * Built with avr-gcc with Y reserved (the firmware keeps its data stack
 * there), so no function here may need a stack frame: all buffers are in
 * the state structure.
 */
#include "target_p10r.h"

typedef unsigned char u8;
typedef signed char s8;
typedef unsigned short u16;
typedef short s16;
typedef unsigned long u32;

#define FAR const __memx        /* constants live in flash above 64K */
#define NOINLINE __attribute__((noinline))  /* keeps every function frameless */

u8 fw_text(char *s, u8 y, u8 x);
u8 fw_text_inv(char *s, u8 y, u8 x);
void fw_list_draw(u8 item);
u8 fw_scan_step(u16 dwell_ms, u16 *adc, u32 khz);
void fw_delay_ms(u32 ms);
void fw_set_freq(u32 khz);
void fw_scan_prepare(void);
void fw_scan_finish(void);
void fw_audio_mute(void);
void fw_clear_group_channel(void);
void fw_redraw(void);
void fw_text_far(const __memx char *s, u8 y, u8 x, u8 inverted);

#define RAM8(a)         (*(volatile u8 *)(a))
#define RAM32(a)        (*(volatile u32 *)(a))

#define ST_FIND         160         /* FIND IEMS selected in the RADIO list */
#define ST_SCANNING     161
#define ST_RESULTS      162
#define NOT_OURS        0xff

#define STEP_KHZ        25
#define MAX_CHANNELS    3201
#define MAX_CAND        32
#define MAX_RESULTS     32
#define DWELL_MS        5           /* as the stock full scan */
#define PEAK_MARGIN_DB  12          /* above the noise floor */
#define PEAK_MIN_DBM    (-95)
#define MAX_WIDTH_CH    24          /* -6 dB width above 600 kHz: not an IEM */
#define GUARD_CH        8           /* carriers are at least 200 kHz apart */
#define SKIRT_RISE_DB   3           /* a rise this big is another carrier */
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
    u8 phase;
    u8 ncand, nres;
    u8 vi;                          /* candidate being verified */
    u8 cursor;
    u8 tuned;                       /* result we are tuned to, 0xff none */
    u16 idx, count;
    s16 mq;                         /* marquee position */
    u32 lo;
    u16 adc;
    char line[20];
    u16 cand[MAX_CAND];
    s8 cand_dbm[MAX_CAND];
    struct result res[MAX_RESULTS];
    u16 hist[256];
    s8 rssi[MAX_CHANNELS];
};

/* The stock full scan's sample buffer is idle outside a scan.  Its saved
 * spectrum is lost; the scan state is marked empty when we are done. */
#define S ((struct state *)RAM_SCAN_BUFFER)

/* ---- marquee ------------------------------------------------------- */

static FAR char marquee_text[] = "DOING THE MAGIC!";
#define MQ_LEN      16
#define MQ_SCALE    3
#define MQ_PITCH    18
#define MQ_Y        9
#define MQ_SPEED    6
#define LCD_W       80

/* 5x7 glyphs, one byte per row, bit 4 = left column */
static FAR u8 glyphs[][8] = {
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
#define NGLYPHS     12

/* the redraw has cleared the frame buffer and will flush it afterwards */
NOINLINE static void marquee_frame(void)
{
    u8 *fb = (u8 *)RAM_FRAMEBUFFER;
    u8 x;

    for (x = 0; x < LCD_W; x++, fb++) {
        s16 sx = x + S->mq - LCD_W;         /* column of the text strip */
        u8 col, g, row, ch;

        if (sx < 0 || sx >= MQ_LEN * MQ_PITCH)
            continue;
        col = (u8)(sx % MQ_PITCH) / MQ_SCALE;
        if (col >= 5)
            continue;                       /* gap between characters */
        ch = marquee_text[sx / MQ_PITCH];
        for (g = 0; g < NGLYPHS; g++)
            if (glyphs[g][0] == ch)
                break;
        if (g == NGLYPHS)
            continue;                       /* space */
        for (row = 0; row < 7; row++) {
            u8 y = MQ_Y + row * MQ_SCALE, k;
            if (!(glyphs[g][1 + row] & (0x10 >> col)))
                continue;
            for (k = y; k < y + MQ_SCALE; k++)
                fb[(k >> 3) * LCD_W] |= 1 << (k & 7);
        }
    }
    S->mq += MQ_SPEED;
    if (S->mq >= LCD_W + MQ_LEN * MQ_PITCH)
        S->mq = 0;
}

/* ---- radio ----------------------------------------------------------- */

static s8 to_dbm(u16 adc)
{
    s16 v = ((s16)adc - RSSI_AT_M90) / RSSI_PER_DB - 90;
    if (v < -127)
        v = -127;
    if (v > 127)
        v = 127;
    return v;
}

NOINLINE static s8 measure(u32 khz)      /* tunes; -128 if the step failed */
{
    if (!fw_scan_step(DWELL_MS, &S->adc, khz))
        return -128;
    return to_dbm(S->adc);
}

static u8 pilot_present(void)
{
    return !(RAM8(IO_PILOT_PORT) & (1 << IO_PILOT_BIT));
}

NOINLINE static void scan_start(void)
{
    const __flash u8 *band = (const __flash u8 *)(*(u16 *)RAM_BAND);
    u32 lo = *(const __flash u32 *)(band + BAND_LOW);
    u32 hi = *(const __flash u32 *)(band + BAND_HIGH);
    u32 n = (hi - lo) / STEP_KHZ + 1;

    S->lo = lo;
    S->count = n > MAX_CHANNELS ? MAX_CHANNELS : n;
    S->idx = 0;
    S->ncand = S->nres = 0;
    S->vi = 0;
    S->cursor = 0;
    S->tuned = 0xff;
    S->mq = 0;
    S->phase = PH_SWEEP;
    RAM8(RAM_SCAN) = 0;             /* the stock spectrum is gone */
    fw_scan_prepare();
}

/* Pick the carriers out of the sweep: strongest first, each one centred
 * between its -6 dB points; anything wider than an FM carrier is skipped. */
NOINLINE static void find_candidates(void)
{
    s8 *r = S->rssi;
    s16 n = S->count, i, l, h, c, p;
    s8 thr, best, k;
    u16 acc;

    for (i = 0; i < 256; i++)
        S->hist[i] = 0;
    for (i = 0; i < n; i++)
        S->hist[(u8)(r[i] + 128)]++;
    acc = 0;
    for (i = 1; i < 256; i++) {         /* lower quartile, bin 0 = no data */
        acc += S->hist[i];
        if (acc >= (n - S->hist[0]) / 4)
            break;
    }
    i += PEAK_MARGIN_DB - 128;
    thr = i < PEAK_MIN_DBM ? PEAK_MIN_DBM : i > 127 ? 127 : i;

    while (S->ncand < MAX_CAND) {
        best = -128;
        p = -1;
        for (i = 0; i < n; i++)
            if (r[i] > best) {
                best = r[i];
                p = i;
            }
        if (p < 0 || best < thr)
            break;
        k = best < -122 ? -128 : best - 6;
        for (l = p; l > 0 && r[l - 1] >= k; l--)
            ;
        for (h = p; h < n - 1 && r[h + 1] >= k; h++)
            ;
        c = (l + h) / 2;
        if (h - l + 1 <= MAX_WIDTH_CH) {
            u8 j;
            /* keep the list sorted by frequency */
            for (j = S->ncand; j > 0 && S->cand[j - 1] > (u16)c; j--) {
                S->cand[j] = S->cand[j - 1];
                S->cand_dbm[j] = S->cand_dbm[j - 1];
            }
            S->cand[j] = c;
            S->cand_dbm[j] = best;
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
 * step off) and wait for the stereo decoder to report the pilot. */
NOINLINE static void verify_candidate(void)
{
    static FAR s8 offs[] = { 0, 1, -1 };
    u8 o, hits;
    u16 t;

    for (o = 0; o < 3; o++) {
        s16 ch = S->cand[S->vi] + offs[o];
        u32 khz;
        s8 dbm;

        if (ch < 0 || ch >= (s16)S->count)
            continue;
        khz = S->lo + (u32)ch * STEP_KHZ;
        dbm = measure(khz);
        if (dbm == -128)
            continue;
        hits = 0;
        for (t = 0; t < PILOT_WAIT_MS && hits < PILOT_HITS; t += PILOT_POLL_MS) {
            fw_delay_ms(PILOT_POLL_MS);
            hits = pilot_present() ? hits + 1 : 0;
        }
        if (hits >= PILOT_HITS) {
            if (S->nres < MAX_RESULTS) {
                S->res[S->nres].khz = khz;
                S->res[S->nres].dbm = dbm;
                S->nres++;
            }
            return;
        }
        /* no carrier left here at all: do not bother with the neighbours */
        if (dbm < S->cand_dbm[S->vi] - 20)
            return;
    }
}

/* ---- results list ---------------------------------------------------- */

NOINLINE static char *put_uint(char *p, u16 v, u8 digits)    /* digits 0: as needed */
{
    u8 n = 0, i;
    u16 d = 10000;

    for (i = 5; i; i--, d /= 10) {
        u8 q = v / d;
        v -= q * d;
        if (q || n || i <= digits || i == 1) {
            *p++ = '0' + q;
            n = 1;
        }
    }
    return p;
}

NOINLINE static char *put_str(char *p, FAR char *s)
{
    while (*s)
        *p++ = *s++;
    return p;
}

NOINLINE static void results_row(u8 i, u8 y)
{
    char *p;
    s8 dbm = S->res[i].dbm;
    u16 mhz = S->res[i].khz / 1000;

    p = put_uint(S->line, i + 1, 0);
    *p++ = '.';
    p = put_uint(p, mhz, 0);
    *p++ = '.';
    p = put_uint(p, S->res[i].khz - (u32)mhz * 1000, 3);
    *p++ = i == S->tuned ? '*' : ' ';
    if (dbm < 0) {
        *p++ = '-';
        dbm = -dbm;
    }
    p = put_uint(p, dbm, 0);
    *p = 0;
    if (i == S->cursor)
        fw_text_inv(S->line, y, 0);
    else
        fw_text(S->line, y, 0);
}

NOINLINE static void results_draw(void)
{
    static FAR char title[] = "RESULTS", none[] = "NO IEMS FOUND";
    char *line = S->line, *p;
    u8 top, i;

    p = put_str(line, title);
    if (S->nres) {
        *p++ = ' ';
        p = put_uint(p, S->cursor + 1, 0);
        *p++ = '/';
        p = put_uint(p, S->nres, 0);
    }
    *p = 0;
    fw_text(line, 0, 0);
    if (!S->nres) {
        fw_text_far(none, 15, 0, 0);
        return;
    }
    top = S->cursor / ROWS * ROWS;
    for (i = top; i < top + ROWS && i < S->nres; i++)
        results_row(i, 10 + (i - top) * 10);
}

NOINLINE static void results_connect(void)
{
    u32 khz;

    if (!S->nres || S->cursor >= MAX_RESULTS)
        return;
    khz = S->res[S->cursor].khz;
    /* what the frequency menu does when a new value is confirmed */
    RAM8(RAM_TUNING_1) = 1;
    RAM8(RAM_TUNING_2) = 1;
    RAM8(RAM_TUNING_3) = 1;
    fw_audio_mute();
    fw_clear_group_channel();
    fw_set_freq(khz);
    RAM32(RAM_EDIT_FREQ) = khz;
    RAM8(RAM_EDIT_GROUP) = 0xff;
    RAM8(RAM_EDIT_CHANNEL) = 0xff;
    fw_delay_ms(200);
    RAM8(RAM_TUNING_1) = 0;
    S->tuned = S->cursor;
}

/* ---- hooks (called through glue_p10r.S) ---------------------------------- */

/* -> NOT_OURS to let the firmware's tables handle the event */
u8 c_sm_event(u8 *state, u8 ev)
{
    u8 s = *state;

    /* events every menu screen gives way to: let the firmware's tables
     * handle them as if we were on the neighbouring list item */
    if (s >= ST_FIND && (ev == EV_GLOBAL_1 || ev == EV_GLOBAL_2)) {
        if (s == ST_SCANNING)
            fw_scan_finish();
        *state = ST_LIST_LAST;
        return NOT_OURS;
    }
    if (s == ST_LIST_LAST && ev == EV_DOWN) {
        *state = ST_FIND;
    } else if (s == ST_LIST_FIRST && ev == EV_UP) {
        *state = ST_FIND;
    } else if (s == ST_FIND) {
        if (ev == EV_UP) {
            *state = ST_LIST_LAST;
        } else if (ev == EV_DOWN) {
            *state = ST_LIST_FIRST;
        } else if (ev == EV_EXIT) {
            *state = ST_MAIN_RADIO;
        } else if (ev == EV_ENTER) {
            scan_start();
            *state = ST_SCANNING;
        } else {
            return 0;
        }
    } else if (s == ST_SCANNING) {
        if (ev != EV_EXIT)
            return 0;
        fw_scan_finish();
        *state = ST_FIND;
    } else if (s == ST_RESULTS) {
        if (ev == EV_EXIT) {
            *state = ST_FIND;
        } else if (ev == EV_ENTER) {
            results_connect();
        } else if (S->nres && ev == EV_UP) {
            S->cursor = S->cursor ? S->cursor - 1 : S->nres - 1;
        } else if (S->nres && ev == EV_DOWN) {
            S->cursor = S->cursor + 1 < S->nres ? S->cursor + 1 : 0;
        } else {
            return 0;
        }
    } else {
        return NOT_OURS;
    }
    fw_redraw();
    return 1;
}

/* Runs in the redraw after the frame buffer was cleared; returns the state
 * the firmware's own drawing code switches on (ours fall through to the
 * final flush). */
u8 c_draw(u8 *state)
{
    static FAR char find[] = "FIND IEMS";
    u8 s = *state;

    if (s == ST_SCANNING) {
        marquee_frame();
    } else if (s == ST_RESULTS) {
        results_draw();
    } else if (s == ST_FIND || s == ST_LIST_RFPAD || s == ST_LIST_LAST) {
        if (s == ST_FIND)
            fw_list_draw(LIST_PAGE2_NO_SELECTION);
        fw_text_far(find, 20, LIST_X, s == ST_FIND);
    }
    return s;
}

/* every pass of the main loop */
void c_poll(void)
{
    if (RAM8(RAM_SM_STATE) != ST_SCANNING)
        return;
    if (S->phase == PH_SWEEP) {
        S->rssi[S->idx] = measure(S->lo + (u32)S->idx * STEP_KHZ);
        if (++S->idx >= S->count) {
            find_candidates();
            S->phase = PH_VERIFY;
        }
    } else if (S->vi < S->ncand) {
        verify_candidate();
        S->vi++;
    } else {
        fw_scan_finish();
        RAM8(RAM_SM_STATE) = ST_RESULTS;
    }
    RAM8(RAM_REDRAW_FLAG) = 1;
}
