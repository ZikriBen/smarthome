"""Measure whether a sparse (Cues-driven) subtitle read would beat a full read.

    python -m tests.mkv_layout file.mkv [file2.mkv ...]

Walks a LOCAL Matroska file and reports: Cues entries per track (and whether
subtitle CuePoints carry CueRelativePosition), and the Telegram-chunk cost
of extracting every text-subtitle block via (a) a full sequential read, (b)
Cues-driven reads of only the chunks holding subtitle blocks, (c) walking all
block headers (cluster-by-cluster header hopping). Telegram reads are whole
aligned chunks, so any touched byte costs its whole chunk.
"""
import sys

from app.stream.ranges import CHUNK_SIZE
from app.stream.subs import ID_BLOCK, ID_BLOCKGROUP, ID_CLUSTER, ID_SEGMENT, ID_SIMPLEBLOCK, ID_TRACKS, parse_tracks, read_vint

ID_CUES, ID_CUEPOINT, ID_CUETRACKPOS = 0x1C53BB6B, 0xBB, 0xB7
ID_CUETRACK, ID_CUECLUSTERPOS, ID_CUERELPOS = 0xF7, 0xF1, 0xF0


def elements(buf, start, end):
    pos = start
    while pos < end:
        eid, p = read_vint(buf, pos, True)
        size, p = read_vint(buf, p, False)
        if size == (1 << (7 * (p - pos - 1 if False else 1))) - 1:
            pass
        yield eid, pos, p, size
        if size < 0 or p + size > len(buf) and eid != ID_SEGMENT:
            return
        pos = p + size


def uint(b):
    return int.from_bytes(b, "big") if b else 0


def runs(chunks):
    out, prev = 0, None
    for c in sorted(chunks):
        if prev is None or c != prev + 1:
            out += 1
        prev = c
    return out


def analyse(path):
    buf = open(path, "rb").read()
    n_chunks = -(-len(buf) // CHUNK_SIZE)
    eid, p = read_vint(buf, 0, True)
    size, p = read_vint(buf, p, False)
    pos = p + size  # skip EBML header
    eid, p = read_vint(buf, pos, True)
    assert eid == ID_SEGMENT
    seg_size, seg_data = read_vint(buf, p, False)
    seg_end = min(len(buf), seg_data + seg_size) if seg_size > 0 else len(buf)
    tracks, text_nums = [], set()
    cue_tracks, cue_rel, cue_sub_pos = {}, 0, []
    clusters = 0
    sub_blocks, all_block_hdr_chunks, cluster_hdr_chunks = [], set(), set()
    pos = seg_data
    while pos < seg_end:
        eid, p = read_vint(buf, pos, True)
        size, p = read_vint(buf, p, False)
        if eid == ID_TRACKS:
            tracks = parse_tracks(buf[p:p + size])
            text_nums = {t.number for t in tracks}
        elif eid == ID_CUES:
            for cid, cs, cp, csz in elements(buf, p, p + size):
                if cid != ID_CUEPOINT:
                    continue
                for tid, ts_, tp, tsz in elements(buf, cp, cp + csz):
                    if tid != ID_CUETRACKPOS:
                        continue
                    f = {e: buf[q:q + s] for e, _, q, s in elements(buf, tp, tp + tsz)}
                    tn = uint(f.get(ID_CUETRACK))
                    cue_tracks[tn] = cue_tracks.get(tn, 0) + 1
                    if tn in text_nums:
                        if ID_CUERELPOS in f:
                            cue_rel += 1
                        cue_sub_pos.append(seg_data + uint(f.get(ID_CUECLUSTERPOS)) + uint(f.get(ID_CUERELPOS)))
        elif eid == ID_CLUSTER:
            clusters += 1
            cluster_hdr_chunks.add(pos // CHUNK_SIZE)
            for bid, bs, bp, bsz in elements(buf, p, p + size):
                if bid in (ID_SIMPLEBLOCK, ID_BLOCKGROUP):
                    all_block_hdr_chunks.add(bs // CHUNK_SIZE)
                    data = buf[bp:bp + bsz]
                    if bid == ID_BLOCKGROUP:
                        inner = [(q, s) for e, _, q, s in elements(buf, bp, bp + bsz) if e == ID_BLOCK]
                        if not inner:
                            continue
                        q, s = inner[0]
                        data, bp, bsz = buf[q:q + s], q, s
                    tn, _ = read_vint(data, 0, False)
                    if tn in text_nums:
                        sub_blocks.append((bs, bp + bsz))
        pos = p + size
    sub_chunks = set()
    for a, b in sub_blocks:
        sub_chunks.update(range(a // CHUNK_SIZE, (b - 1) // CHUNK_SIZE + 1))
    print(f"\n{path}: {len(buf) / 1048576:.1f} MB = {n_chunks} chunks of {CHUNK_SIZE >> 10} KB, {clusters} clusters")
    print("  text sub tracks:", [(t.number, t.lang, t.codec if hasattr(t, 'codec') else '') for t in tracks])
    print("  Cues entries per track:", dict(sorted(cue_tracks.items())),
          f"| subtitle CuePoints with CueRelativePosition: {cue_rel}")
    print(f"  subtitle blocks: {len(sub_blocks)}; chunks holding them: {len(sub_chunks)} "
          f"({100 * len(sub_chunks) / n_chunks:.0f}% of file), contiguous runs (range requests): {runs(sub_chunks)}")
    print(f"  (a) full read: {n_chunks} chunks")
    if cue_sub_pos:
        print(f"  (b) Cues-driven: {len(sub_chunks)} chunks + Cues element (sub cues cover "
              f"{100 * len(cue_sub_pos) / max(1, len(sub_blocks)):.0f}% of blocks)")
    else:
        print("  (b) Cues-driven: impossible, Cues index no subtitle track")
    print(f"  (c) header hopping: {len(all_block_hdr_chunks | cluster_hdr_chunks)} chunks "
          f"({100 * len(all_block_hdr_chunks | cluster_hdr_chunks) / n_chunks:.0f}%)")


for f in sys.argv[1:]:
    analyse(f)
