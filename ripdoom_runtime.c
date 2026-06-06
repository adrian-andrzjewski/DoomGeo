/* ripdoom_runtime.c - query real Doom geometry converted by RIPDOOM-lite. */
#include "ripdoom_runtime.h"

enum { RIPDOOM_BLOCKMAP_SHIFT = 7 };

int ripdoom_point_side(short x, short y, const NgRipNode *node) {
    long dx;
    long dy;
    long left;
    long right;
    if (!node) return 0;

    if (node->dx == 0) {
        if (x <= node->x) return node->dy > 0;
        return node->dy < 0;
    }
    if (node->dy == 0) {
        if (y <= node->y) return node->dx < 0;
        return node->dx > 0;
    }

    dx = (long)x - node->x;
    dy = (long)y - node->y;
    left = (long)node->dy * dx;
    right = dy * (long)node->dx;
    return right < left ? 0 : 1;
}

int ripdoom_point_subsector(short x, short y) {
    unsigned short node_index;
    unsigned short guard;
    if (NG_RIP_SUBSECTOR_COUNT == 0) return -1;
    if (NG_RIP_NODE_COUNT == 0) return 0;

    node_index = (unsigned short)(NG_RIP_NODE_COUNT - 1);
    for (guard = 0; guard <= NG_RIP_NODE_COUNT; guard++) {
        const NgRipNode *node = &g_rip_nodes[node_index];
        unsigned short child = node->child[ripdoom_point_side(x, y, node) ? 1 : 0];
        unsigned short child_index = (unsigned short)(child & 0x7FFF);
        if (child & 0x8000) {
            if (child_index >= NG_RIP_SUBSECTOR_COUNT) return -1;
            return (int)child_index;
        }
        if (child_index >= NG_RIP_NODE_COUNT) return -1;
        node_index = child_index;
    }
    return -1;
}

int ripdoom_point_sector(short x, short y) {
    int subsector = ripdoom_point_subsector(x, y);
    int sector;
    if (subsector < 0 || subsector >= NG_RIP_SUBSECTOR_COUNT) return -1;
    sector = g_rip_subsectors[subsector].sector;
    if (sector < 0 || sector >= NG_RIP_SECTOR_COUNT) return -1;
    return sector;
}

int ripdoom_blockmap_cell(short x, short y, int *block_x, int *block_y) {
    long rel_x = (long)x - NG_RIP_BLOCKMAP_ORIGIN_X;
    long rel_y = (long)y - NG_RIP_BLOCKMAP_ORIGIN_Y;
    int bx;
    int by;
    if (rel_x < 0 || rel_y < 0) return 0;
    bx = (int)(rel_x >> RIPDOOM_BLOCKMAP_SHIFT);
    by = (int)(rel_y >> RIPDOOM_BLOCKMAP_SHIFT);
    if (bx < 0 || by < 0 || bx >= NG_RIP_BLOCKMAP_W || by >= NG_RIP_BLOCKMAP_H) return 0;
    if (block_x) *block_x = bx;
    if (block_y) *block_y = by;
    return 1;
}

int ripdoom_blockmap_line_count(int block_x, int block_y) {
    return ripdoom_blockmap_lines(block_x, block_y, 0, 0);
}

static int ripdoom_append_unique(unsigned short value, unsigned short *out_values, int count, int max_values) {
    int i;
    int stored = count < max_values ? count : max_values;
    for (i = 0; i < stored; i++) {
        if (out_values[i] == value) return count;
    }
    if (count < max_values) out_values[count] = value;
    return count + 1;
}

int ripdoom_blockmap_lines(int block_x, int block_y, unsigned short *out_lines, int max_lines) {
    int cell;
    int offset;
    int count = 0;
    if (block_x < 0 || block_y < 0 || block_x >= NG_RIP_BLOCKMAP_W || block_y >= NG_RIP_BLOCKMAP_H) return 0;
    cell = block_y * NG_RIP_BLOCKMAP_W + block_x;
    offset = g_rip_blockmap_words[4 + cell];
    if (offset < 0 || offset >= NG_RIP_BLOCKMAP_WORD_COUNT) return 0;
    if (g_rip_blockmap_words[offset] == 0) offset++;
    while (offset < NG_RIP_BLOCKMAP_WORD_COUNT && g_rip_blockmap_words[offset] != -1) {
        int line = g_rip_blockmap_words[offset++];
        if (line >= 0 && line < NG_RIP_LINE_COUNT) {
            if (out_lines && count < max_lines) out_lines[count] = (unsigned short)line;
            count++;
        }
    }
    return count;
}

int ripdoom_collect_local_lines(short x, short y, int block_radius, unsigned short *out_lines, int max_lines) {
    int center_x;
    int center_y;
    int count = 0;
    int by;
    if (!out_lines || max_lines <= 0) return 0;
    if (block_radius < 0) block_radius = 0;
    if (!ripdoom_blockmap_cell(x, y, &center_x, &center_y)) return 0;

    for (by = center_y - block_radius; by <= center_y + block_radius; by++) {
        int bx;
        for (bx = center_x - block_radius; bx <= center_x + block_radius; bx++) {
            int cell;
            int offset;
            if (bx < 0 || by < 0 || bx >= NG_RIP_BLOCKMAP_W || by >= NG_RIP_BLOCKMAP_H) continue;
            cell = by * NG_RIP_BLOCKMAP_W + bx;
            offset = g_rip_blockmap_words[4 + cell];
            if (offset < 0 || offset >= NG_RIP_BLOCKMAP_WORD_COUNT) continue;
            if (g_rip_blockmap_words[offset] == 0) offset++;
            while (offset < NG_RIP_BLOCKMAP_WORD_COUNT && g_rip_blockmap_words[offset] != -1) {
                int line = g_rip_blockmap_words[offset++];
                if (line >= 0 && line < NG_RIP_LINE_COUNT) {
                    count = ripdoom_append_unique((unsigned short)line, out_lines, count, max_lines);
                }
            }
        }
    }
    return count > max_lines ? max_lines : count;
}

int ripdoom_collect_local_segs(short x, short y, int block_radius, unsigned short *out_segs, int max_segs) {
    enum { LOCAL_LINE_LIMIT = 96 };
    unsigned short lines[LOCAL_LINE_LIMIT];
    int line_count;
    int seg_count = 0;
    int line_index;
    if (!out_segs || max_segs <= 0) return 0;
    line_count = ripdoom_collect_local_lines(x, y, block_radius, lines, LOCAL_LINE_LIMIT);
    for (line_index = 0; line_index < line_count; line_index++) {
        const NgRipLineSegSpan *span;
        int span_index;
        unsigned short line = lines[line_index];
        if (line >= NG_RIP_LINE_COUNT) continue;
        span = &g_rip_line_seg_spans[line];
        if ((int)span->firstseg + (int)span->numsegs > NG_RIP_LINE_SEG_INDEX_COUNT) continue;
        for (span_index = 0; span_index < span->numsegs; span_index++) {
            unsigned short seg = g_rip_line_seg_indices[span->firstseg + span_index];
            if (seg < NG_RIP_SEG_COUNT) {
                seg_count = ripdoom_append_unique(seg, out_segs, seg_count, max_segs);
            }
        }
    }
    return seg_count > max_segs ? max_segs : seg_count;
}
