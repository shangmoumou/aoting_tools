/**
 * bag.js —— 零依赖 ROS bag (v2.0) 解析 + ROS1 消息反序列化
 *
 * 纯手写，不依赖任何 CDN / npm 包，可完全离线运行。
 * 同时可在浏览器和 Node 里跑（用于测试）。
 *
 * ── ROS bag 文件结构速查 ──────────────────────────────
 *   文件头 13 字节: "#ROSBAG V2.0\n"
 *   之后全是 record，每条 record:
 *     uint32 header_len | header(header_len) | uint32 data_len | data
 *   header 内部是若干  uint32 field_len | "name=value"
 *   靠 header 里的 op 字段区分类型:
 *     0x02 MessageData  0x03 BagHeader  0x04 IndexData
 *     0x05 Chunk        0x06 ChunkInfo  0x07 Connection
 * ───────────────────────────────────────────────────
 */

// ============ 基础: 字节读取 ============

class Reader {
  constructor(buf, off = 0) { this.buf = buf; this.off = off; this.dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength); }
  u8() { return this.buf[this.off++]; }
  i8() { return (this.buf[this.off++] << 24) >> 24; }
  u16() { const v = this.dv.getUint16(this.off, true); this.off += 2; return v; }
  i16() { const v = this.dv.getInt16(this.off, true); this.off += 2; return v; }
  u32() { const v = this.dv.getUint32(this.off, true); this.off += 4; return v; }
  i32() { const v = this.dv.getInt32(this.off, true); this.off += 4; return v; }
  f32() { const v = this.dv.getFloat32(this.off, true); this.off += 4; return v; }
  f64() { const v = this.dv.getFloat64(this.off, true); this.off += 8; return v; }
  u64() { const v = this.dv.getBigUint64(this.off, true); this.off += 8; return v; }
  i64() { const v = this.dv.getBigInt64(this.off, true); this.off += 8; return v; }
  bytes(n) { const v = this.buf.subarray(this.off, this.off + n); this.off += n; return v; }
  skip(n) { this.off += n; }
}

const td = new TextDecoder('utf-8');

/** 解析 record 的 header 段 -> { 字段名的字节串: value 的字节串 } */
function parseHeaderFields(raw) {
  const out = Object.create(null);
  const r = new Reader(raw);
  while (r.off < raw.length) {
    const len = r.u32();
    const kv = r.bytes(len);
    let eq = -1;
    for (let i = 0; i < kv.length; i++) if (kv[i] === 0x3d) { eq = i; break; } // '='
    if (eq < 0) continue;
    out[td.decode(kv.subarray(0, eq))] = kv.subarray(eq + 1);
  }
  return out;
}

function hStr(f, k) { return f[k] ? td.decode(f[k]) : undefined; }
function hU32(f, k) { return f[k] ? new DataView(f[k].buffer, f[k].byteOffset, 4).getUint32(0, true) : undefined; }
function hU8(f, k) { return f[k] ? f[k][0] : undefined; }

/** 从 buf 的 off 处解析一条 record，返回 {op, header, data, totalLen} */
function parseRecord(buf, off) {
  if (off + 8 > buf.length) return null;
  const dv = new DataView(buf.buffer, buf.byteOffset + off, buf.byteLength - off);
  const hlen = dv.getUint32(0, true);
  const hstart = off + 4;
  if (hstart + hlen + 4 > buf.length) return null;
  const header = parseHeaderFields(buf.subarray(hstart, hstart + hlen));
  const dlenOff = hstart + hlen;
  const dlen = dv.getUint32(hlen + 4, true);
  const dstart = dlenOff + 4;
  if (dstart + dlen > buf.length) return null;
  return {
    op: hU8(header, 'op'),
    header,
    data: buf.subarray(dstart, dstart + dlen),
    totalLen: 4 + hlen + 4 + dlen,
    dataOffset: dstart,
  };
}

// ============ LZ4 解压（纯 JS，支持 frame / block 两种格式） ============
//
// 三个必须踩过的坑：
//   1) ROS 生态里 compression=lz4 有两种写法：
//      · frame 格式（带 04 22 4D 18 magic）—— rosbags 与多数 ROS1 实现用它
//      · 裸 block 格式 —— 少数实现用它
//      只写 block 时，frame 的 magic 会被当成压缩数据，
//      解出 offset=0x4D22 这种值 -> "LZ4: bad offset"。
//   2) frame 内部按块切分，且默认 block_linked=True：
//      后面的块可以引用前面块的内容（64KB 滑动窗口）。
//      每块都从空白开始解会失败，必须带着历史窗口解。
//   3) 解压后长度未知，缓冲区要能自动扩容。

const LZ4_WINDOW = 65536;   // 跨块引用的最大回溯窗口

/**
 * 块格式解压
 * @param src      压缩数据
 * @param hint     预估解压后大小（不足会自动扩容）
 * @param history  前一块末尾的数据，作为可回溯的历史（linked block 时用）
 */
export function lz4BlockDecompress(src, hint, history) {
  const pre = history || new Uint8Array(0);
  let cap = Math.max(hint || (1 << 20), 4096) + pre.length;
  let dst = new Uint8Array(cap);
  if (pre.length) dst.set(pre, 0);
  let si = 0, di = pre.length;

  const ensure = n => {
    if (di + n <= dst.length) return;
    let c = dst.length;
    while (c < di + n) c *= 2;
    const nd = new Uint8Array(c);
    nd.set(dst.subarray(0, di));
    dst = nd;
  };

  while (si < src.length) {
    const token = src[si++];
    let litLen = token >> 4;
    if (litLen === 15) {
      let b;
      do { b = si < src.length ? src[si++] : 0; litLen += b; } while (b === 255);
    }
    if (litLen > 0) {
      ensure(litLen);
      dst.set(src.subarray(si, si + litLen), di);
      si += litLen; di += litLen;
    }
    if (si >= src.length) break;            // 最后一段只有字面量

    const offset = src[si++] | (src[si++] << 8);
    let matchLen = token & 0x0f;
    if (matchLen === 15) {
      let b;
      do { b = si < src.length ? src[si++] : 0; matchLen += b; } while (b === 255);
    }
    matchLen += 4;                          // MINMATCH

    if (offset === 0 || offset > di) {
      throw new Error(`LZ4 解压失败: offset=${offset} 超出已解出的 ${di} 字节`);
    }
    ensure(matchLen);
    let from = di - offset;
    for (let i = 0; i < matchLen; i++) dst[di++] = dst[from++];
  }
  return dst.subarray(pre.length, di);
}

/** 把新解出的 out 并入历史窗口，只保留末尾 64KB */
function lz4PushHistory(history, out) {
  if (out.length >= LZ4_WINDOW) return out.subarray(out.length - LZ4_WINDOW).slice();
  const need = LZ4_WINDOW - out.length;
  const keep = history.length > need ? history.subarray(history.length - need) : history;
  const nh = new Uint8Array(keep.length + out.length);
  nh.set(keep, 0);
  nh.set(out, keep.length);
  return nh;
}

/** frame 格式解压（跳过各类校验和） */
export function lz4FrameDecompress(src) {
  if (src[0] !== 0x04 || src[1] !== 0x22 || src[2] !== 0x4d || src[3] !== 0x18) {
    throw new Error('不是 LZ4 frame 格式');
  }
  const FLG = src[4], BD = src[5];
  const linked = (FLG & 0x20) === 0;       // bit5=0 表示块之间有关联
  let p = 6;
  if (FLG & 0x08) p += 8;                  // Content Size
  if (FLG & 0x01) p += 4;                  // DictID（不支持，跳过）
  p += 1;                                  // 头部校验和

  const maxBlock = 1 << (2 * ((BD >> 4) & 0x7) + 8);   // 4=64KB 5=256KB 6=1MB 7=4MB

  const parts = [];
  let total = 0, history = new Uint8Array(0);

  while (p + 4 <= src.length) {
    const bs = new DataView(src.buffer, src.byteOffset + p, 4).getUint32(0, true);
    p += 4;
    if (bs === 0) break;                   // EndMark
    const compressed = (bs & 0x80000000) === 0;
    const size = bs & 0x7fffffff;
    if (p + size > src.length) throw new Error('LZ4 frame 块长度越界');
    const block = src.subarray(p, p + size);
    p += size;

    const out = compressed
      ? lz4BlockDecompress(block, maxBlock, linked ? history : null)
      : block;

    parts.push(out); total += out.length;
    if (linked) history = lz4PushHistory(history, out);
  }

  const dst = new Uint8Array(total);
  let o = 0;
  for (const c of parts) { dst.set(c, o); o += c.length; }
  return dst;
}

/** 自动判断格式并解压 */
export function lz4Decompress(src, uncompressedSize) {
  if (src.length >= 4 && src[0] === 0x04 && src[1] === 0x22 && src[2] === 0x4d && src[3] === 0x18) {
    return lz4FrameDecompress(src);
  }
  return lz4BlockDecompress(src, uncompressedSize || (1 << 22), null);
}

// ============ ROS1 消息定义 (.msg) 解析 ============

const BUILTIN = new Set(['bool', 'int8', 'uint8', 'int16', 'uint16', 'int32', 'uint32',
  'int64', 'uint64', 'float32', 'float64', 'string', 'time', 'duration']);

/**
 * 解析 message_definition 文本
 * @returns { main: 字段数组, types: {类型名: 字段数组} }
 *   字段 = { name, type, isArray, arrayLen(null=变长), isBuiltin, isTime }
 */
export function parseMsgDef(text) {
  const types = Object.create(null);
  let cur = null;
  let mainFields = null;

  for (const rawLine of text.split('\n')) {
    let line = rawLine.replace(/\s+$/, '');
    const hash = line.indexOf('#');
    if (hash >= 0) line = line.slice(0, hash);
    line = line.trim();
    if (!line) continue;

    if (/^=+$/.test(line)) continue;                       // 分隔线
    const m = line.match(/^MSG:\s*(\S+)$/);                // "MSG: pkg/Type"
    if (m) { cur = []; types[m[1]] = cur; if (!mainFields) mainFields = cur; continue; }
    if (line.startsWith('---')) continue;

    if (cur === null) { cur = []; mainFields = cur; }

    // 常量定义: "uint8 STATE_INACTIVE=0" 或 "uint8 X = 1"
    // 记下来供枚举显示用，反序列化时跳过
    const cm = line.match(/^([A-Za-z_][\w/]*)\s+([A-Za-z_]\w*)\s*=\s*(.+)$/);
    if (cm) { cur.push({ name: cm[2], type: cm[1], isConst: true, constValue: cm[3].trim() }); continue; }

    const fm = line.match(/^([A-Za-z_][\w/]*)\s*(\[\s*(\d*)\s*\])?\s+([A-Za-z_]\w*)$/);
    if (!fm) continue;
    const [, baseType, bracket, arrLen, name] = fm;
    cur.push({
      name,
      type: baseType,
      isArray: !!bracket,
      arrayLen: bracket ? (arrLen === '' ? null : parseInt(arrLen, 10)) : null,
    });
  }
  return { main: mainFields || [], types };
}

// ============ ROS1 序列化 -> JS 对象 ============

function typeSize(t) {
  switch (t) {
    case 'bool': case 'int8': case 'uint8': return 1;
    case 'int16': case 'uint16': return 2;
    case 'int32': case 'uint32': case 'float32': return 4;
    case 'int64': case 'uint64': case 'float64': return 8;
    default: return -1;
  }
}
const ARR_CTOR = {
  int8: Int8Array, uint8: Uint8Array, int16: Int16Array, uint16: Uint16Array,
  int32: Int32Array, uint32: Uint32Array, float32: Float32Array, float64: Float64Array,
};

export class MsgDeserializer {
  constructor(msgdefText) {
    const { main, types } = parseMsgDef(msgdefText);
    this.types = types;
    this.main = main.filter(f => !f.isConst);
    this.constants = main.filter(f => f.isConst);
    this._resolved = new Map();
    this.fields = this._resolveFields(this.main, types);
  }

  /** 把字段名解析成可直接读取的 plan，并展开嵌套类型 */
  _resolveFields(fields, types) {
    return fields.map(f => {
      const plan = { name: f.name, isArray: f.isArray, arrayLen: f.arrayLen };
      if (f.isArray) {
        plan.elem = this._resolveType(f.type, types);
      } else {
        Object.assign(plan, this._resolveType(f.type, types));
      }
      return plan;
    });
  }

  _resolveType(tname, types) {
    if (this._resolved.has(tname)) return this._resolved.get(tname);
    const info = { typeName: tname };
    if (BUILTIN.has(tname)) {
      info.kind = tname === 'string' ? 'string' : (tname === 'time' || tname === 'duration') ? 'time' : 'num';
      info.prim = tname;
    } else {
      let fields = types[tname];
      if (!fields) {
        // 尝试剥掉包名再找，或补上包名
        const short = tname.includes('/') ? tname.split('/').pop() : tname;
        for (const k of Object.keys(types)) {
          if (k === tname || k.split('/').pop() === short) { fields = types[k]; info.typeName = k; break; }
        }
      }
      if (!fields) { info.kind = 'unknown'; }
      else {
        info.kind = 'msg';
        info.fields = this._resolveFields(fields.filter(f => !f.isConst), types);
      }
    }
    this._resolved.set(tname, info);
    return info;
  }

  /** 反序列化一条消息 */
  read(bytes) { return this._readMsg(new Reader(bytes), this.fields); }

  _readMsg(r, fields) {
    const out = Object.create(null);
    for (const f of fields) out[f.name] = this._readField(r, f);
    return out;
  }

  _readField(r, f) {
    if (f.isArray) {
      const n = f.arrayLen === null ? r.u32() : f.arrayLen;
      if (f.elem.kind === 'num') {
        const sz = typeSize(f.elem.prim);
        const Ctor = ARR_CTOR[f.elem.prim];
        if (Ctor && sz > 0) {
          // 直接拷贝，快很多（激光 2000 个点就靠这个）
          const need = n * sz;
          const raw = r.bytes(need);
          const copy = new Uint8Array(n * sz);
          copy.set(raw.subarray(0, Math.min(need, raw.length)));
          return new Ctor(copy.buffer);
        }
      }
      const arr = new Array(n);
      for (let i = 0; i < n; i++) arr[i] = this._readOne(r, f.elem);
      return arr;
    }
    return this._readOne(r, f);
  }

  _readOne(r, info) {
    switch (info.kind) {
      case 'num':
        switch (info.prim) {
          case 'bool': return r.u8() !== 0;
          case 'int8': return r.i8();
          case 'uint8': return r.u8();
          case 'int16': return r.i16();
          case 'uint16': return r.u16();
          case 'int32': return r.i32();
          case 'uint32': return r.u32();
          case 'int64': return Number(r.i64());
          case 'uint64': return Number(r.u64());
          case 'float32': return r.f32();
          case 'float64': return r.f64();
          default: r.skip(1); return 0;
        }
      case 'string': {
        const len = r.u32();
        return td.decode(r.bytes(len));
      }
      case 'time': {
        const sec = r.u32(), nsec = r.u32();
        return { sec, nsec, toSec: () => sec + nsec * 1e-9 };
      }
      case 'msg': return this._readMsg(r, info.fields);
      default: return undefined;
    }
  }
}

// ============ Bag 主类 ============

/** 读缓冲区某处的 uint32（小端），自动处理 subarray 的偏移 */
function u32at(b, o) {
  return new DataView(b.buffer, b.byteOffset + o, 4).getUint32(0, true);
}

export class Bag {
  /**
   * @param {object} source 需实现 size 与 read(offset,length)->Promise<Uint8Array>
   */
  constructor(source) {
    this.src = source;
    this.connections = new Map();   // connId -> {id, topic, type, md5sum, msgdef}
    this.chunkInfos = [];           // 仅作记录
    this.entries = [];              // {connId, topic, stamp, bytes}
    this.startTime = null;
    this.endTime = null;
    this.compression = 'none';
    this.stats = Object.create(null);   // op -> 出现次数，用于诊断
    this.indexPos = null;
    // 顶层扫描用的读缓存
    this._cStart = -1;
    this._cBuf = new Uint8Array(0);
  }

  /** 只校验文件头，真正的解析交给 scan() */
  async open() {
    const magic = await this.src.read(0, 13);
    if (td.decode(magic) !== '#ROSBAG V2.0\n') {
      throw new Error('不是有效的 ROS bag v2.0 文件（文件头不是 #ROSBAG V2.0）');
    }
    return this;
  }

  /** 带缓存的读取：优先命中上次读的块，避免逐字节访问文件 */
  async _readAt(off, len) {
    if (off >= this._cStart && off + len <= this._cStart + this._cBuf.length) {
      const s = off - this._cStart;
      return this._cBuf.subarray(s, s + len);
    }
    const want = Math.max(len, 1 << 20);
    const end = Math.min(off + want, this.src.size);
    if (end <= off) return new Uint8Array(0);
    const blk = await this.src.read(off, end - off);
    this._cStart = off;
    this._cBuf = blk;
    return blk.subarray(0, Math.min(len, blk.length));
  }

  /**
   * 顺序扫描整个文件。
   * 为什么不走 index_pos：真实 bag 的头部记录长度不固定（有些实现的 padding
   * 放在 data 段，长度随字段多少变化），按固定窗口读很容易读不全。
   * 顺序扫描对任何合法 bag 都成立，代价只是多读一遍文件。
   */
  async scan(onProgress) {
    const size = this.src.size;
    let off = 13;
    let iter = 0;
    this.stats = Object.create(null);

    while (off + 8 <= size) {
      const h8 = await this._readAt(off, 8);
      if (h8.length < 8) break;
      const hlen = u32at(h8, 0);
      // 合法性检查：header 不可能为 0，也不可能巨大
      if (hlen === 0 || hlen > (1 << 20)) break;

      const hh = await this._readAt(off + 4, hlen + 4);
      if (hh.length < hlen + 4) break;
      const dlen = u32at(hh, hlen);
      const header = parseHeaderFields(hh.subarray(0, hlen));
      const op = hU8(header, 'op');
      this.stats[op] = (this.stats[op] || 0) + 1;

      const dstart = off + 4 + hlen + 4;
      const total = 4 + hlen + 4 + dlen;

      if (op === 0x05) {
        // Chunk：消息都在里面，整块读出来再解
        const cdata = await this.src.read(dstart, dlen);
        this._scanChunk(header, cdata);
      } else if (op === 0x07) {
        // Connection：话题元信息（含消息定义）
        const d = await this.src.read(dstart, dlen);
        const f = parseHeaderFields(d);
        const id = hU32(header, 'conn');
        this.connections.set(id, {
          id,
          topic: hStr(header, 'topic'),
          type: hStr(f, 'type'),
          md5sum: hStr(f, 'md5sum'),
          msgdef: hStr(f, 'message_definition') || '',
        });
      } else if (op === 0x02) {
        // 顶层消息（未分块的 bag 才会出现）
        const d = await this.src.read(dstart, dlen);
        this._push(hU32(header, 'conn'), d, header);
      } else if (op === 0x03) {
        this.indexPos = hU32(header, 'index_pos');
      }
      // 0x04 IndexData / 0x06 ChunkInfo：直接跳过，信息已在 chunk 与 connection 里

      off += total;
      if (total <= 0) break;
      if (onProgress && (++iter % 20 === 0)) onProgress(this.entries.length);
    }

    this._finalize();
    return this.entries.length;
  }

  /** 扫描一个 chunk 内部的消息记录 */
  _scanChunk(cheader, cdata) {
    let data = cdata;
    const comp = hStr(cheader, 'compression') || 'none';
    this.compression = comp;
    if (comp === 'lz4') {
      const size = hU32(cheader, 'size');
      data = lz4Decompress(cdata, size);
    } else if (comp !== 'none') {
      throw new Error(`这个 bag 用了 ${comp} 压缩，当前不支持。请用 rosbag 转成无压缩或 lz4：rosbag decompress xx.bag`);
    }

    let p = 0;
    let guard = 0;
    while (p + 8 <= data.length && guard++ < 2e6) {
      const rec = parseRecord(data, p);
      if (!rec || rec.totalLen <= 0) break;
      if (rec.op === 0x02) {
        this.stats[0x02] = (this.stats[0x02] || 0) + 1;
        this._push(hU32(rec.header, 'conn'), rec.data, rec.header);
      }
      p += rec.totalLen;
    }
  }

  _push(connId, bytes, header) {
    this.entries.push({ connId, stamp: this._readTime(header, 'time'), bytes });
  }

  /** 扫描收尾：补 topic、排序、建索引、算时间范围 */
  _finalize() {
    // connection 记录在文件末尾，扫描时消息先于它出现，所以这里统一补 topic
    for (const e of this.entries) {
      const c = this.connections.get(e.connId);
      e.topic = c ? c.topic : '#' + e.connId;
    }
    this.entries.sort((a, b) => a.stamp - b.stamp);
    if (this.entries.length) {
      this.startTime = this.entries[0].stamp;
      this.endTime = this.entries[this.entries.length - 1].stamp;
    }
    this.byTopic = new Map();
    this.entries.forEach((e, i) => {
      let a = this.byTopic.get(e.topic);
      if (!a) { a = []; this.byTopic.set(e.topic, a); }
      a.push(i);
    });
  }

  _readTime(header, key) {
    const b = header[key];
    if (!b || b.length < 8) return 0;
    const dv = new DataView(b.buffer, b.byteOffset, 8);
    return dv.getUint32(0, true) * 1e9 + dv.getUint32(4, true);
  }

  /** 把全部消息读进内存并建立索引 */
  async loadAll(onProgress) {
    this.entries.length = 0;
    await this.scan(onProgress);
    if (!this.entries.length) {
      throw new Error(`bag 里没读到消息（op 统计: ${JSON.stringify(this.stats)}）`);
    }
    return this.entries.length;
  }

  /** 每个 topic 的反序列化器（懒建 + 缓存） */
  _readers = new Map();
  readerFor(connId) {
    if (this._readers.has(connId)) return this._readers.get(connId);
    const conn = this.connections.get(connId);
    let r = null;
    if (conn && conn.msgdef) {
      try { r = new MsgDeserializer(conn.msgdef); } catch (e) { r = null; }
    }
    this._readers.set(connId, r);
    return r;
  }

  decode(entry) {
    const r = this.readerFor(entry.connId);
    if (!r) return null;
    try { return r.read(entry.bytes); } catch (e) { return null; }
  }

  /** 取 topic 在 t 之前最近的一条 */
  latestBefore(topic, t) {
    const idx = this.byTopic && this.byTopic.get(topic);
    if (!idx || !idx.length) return null;
    let lo = 0, hi = idx.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.entries[idx[mid]].stamp <= t) lo = mid + 1; else hi = mid;
    }
    return lo === 0 ? null : this.entries[idx[lo - 1]];
  }

  topics() { return [...new Set([...this.connections.values()].map(c => c.topic))].sort(); }
}

// ============ TF 坐标变换（2D） ============

/** 四元数 -> 偏航角 */
export function yawOf(q) {
  if (!q) return 0;
  const { x = 0, y = 0, z = 0, w = 1 } = q;
  return Math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z));
}

/** 变换合成: 先做 b 再做 a（结果 p' = a(b(p))） */
export function compose2D(a, b) {
  const c = Math.cos(a.theta), s = Math.sin(a.theta);
  return {
    x: a.x + c * b.x - s * b.y,
    y: a.y + s * b.x + c * b.y,
    theta: a.theta + b.theta,
  };
}

/** 把变换作用到一个点 */
export function apply2D(T, p) {
  const c = Math.cos(T.theta), s = Math.sin(T.theta);
  return { x: T.x + c * p.x - s * p.y, y: T.y + s * p.x + c * p.y };
}

/**
 * TF 树。
 * 为什么必须有它: bag 里各话题的坐标系不一样——
 *   /map           frame_id = map
 *   /odom_origin   frame_id = odom   （里程计，原点在开机位置，不是地图原点）
 *   /scan_raw      frame_id = laser
 *   /scan_radar    frame_id = base_footprint
 * 直接把 odom 坐标画到 map 地图上，必然会整体偏移/旋转，
 * 这正是"地图和点云对不上"的根因。
 */
export class TFTree {
  constructor() {
    this.stat = new Map();   // child -> {parent, x, y, theta}
    this.dyn = new Map();    // child -> [{t, parent, x, y, theta}] 按 t 升序
    this.frames = new Set();
  }

  addStatic(parent, child, x, y, theta) {
    this.stat.set(child, { parent, x, y, theta });
    this.frames.add(parent); this.frames.add(child);
  }

  addDynamic(parent, child, x, y, theta, t) {
    let a = this.dyn.get(child);
    if (!a) { a = []; this.dyn.set(child, a); }
    a.push({ t, parent, x, y, theta });
    this.frames.add(parent); this.frames.add(child);
  }

  finalize() {
    for (const a of this.dyn.values()) a.sort((p, q) => p.t - q.t);
  }

  /** 取 child 在时刻 t 的「到父帧」变换 */
  edgeAt(child, t) {
    const s = this.stat.get(child);
    if (s) return s;                       // 静态优先
    const a = this.dyn.get(child);
    if (!a || !a.length) return null;
    if (t <= a[0].t) return a[0];
    if (t >= a[a.length - 1].t) return a[a.length - 1];
    let lo = 0, hi = a.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (a[mid].t <= t) lo = mid + 1; else hi = mid;
    }
    return a[lo - 1];
  }

  /**
   * 求 target ← source 的变换，即 p_target = T * p_source。
   * 失败返回 null（帧不连通）。
   */
  lookup(target, source, t) {
    if (source === target) return { x: 0, y: 0, theta: 0 };
    let acc = { x: 0, y: 0, theta: 0 };
    let cur = source;
    const seen = new Set();
    while (cur !== target) {
      if (seen.has(cur)) return null;      // 有环
      seen.add(cur);
      const e = this.edgeAt(cur, t);
      if (!e) return null;
      acc = compose2D(e, acc);
      cur = e.parent;
    }
    return acc;
  }

  /** 某帧是否存在且能连到 target */
  canReach(target, frame) { return this.lookup(target, frame, 0) !== null; }
}

/** 浏览器 File -> source */
export class FileSource {
  constructor(file) { this.file = file; this.size = file.size; }
  async read(offset, length) {
    const end = Math.min(offset + length, this.size);
    if (end <= offset) return new Uint8Array(0);
    const buf = await this.file.slice(offset, end).arrayBuffer();
    return new Uint8Array(buf);
  }
}
