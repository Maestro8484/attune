"""Analyze the validation set with the EXPERIMENTAL extended descriptor into a
separate table, then score vs MusicIP ground truth. One self-contained script."""
import os, json, glob, time, sqlite3, numpy as np, concurrent.futures as cf
import features_exp as fx

HERE = os.path.dirname(os.path.abspath(__file__))
GT = os.path.join(HERE, "..", "..", "extracted", "groundtruth")
EXPDB = os.path.join(HERE, "..", "data", "exp.db")
PATHS = os.path.join(HERE, "..", "..", "mixer-ng", "data", "validation_paths.txt")
K = 25


def ensure_db():
    c = sqlite3.connect(EXPDB)
    c.execute("CREATE TABLE IF NOT EXISTS feat(path TEXT PRIMARY KEY, dim INT, vec BLOB)")
    c.commit(); return c


def analyze(workers=6):
    c = ensure_db()
    done = {r[0] for r in c.execute("SELECT path FROM feat")}
    want = [l.strip() for l in open(PATHS, encoding="utf-8") if l.strip()]
    todo = [p for p in want if p not in done]
    print(f"{len(want)} tracks, {len(done)} done, {len(todo)} to analyze")
    if not todo:
        return
    t0 = time.time(); n = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fx.extract, p): p for p in todo}
        for i, f in enumerate(cf.as_completed(futs), 1):
            p = futs[f]; r = f.result()
            if r and "vec" in r:
                c.execute("INSERT OR REPLACE INTO feat VALUES(?,?,?)",
                          (p, r["vec"].shape[0], r["vec"].astype(np.float32).tobytes()))
                n += 1
            if i % 25 == 0 or i == len(todo):
                c.commit()
                rate = i/(time.time()-t0)
                print(f"  {i}/{len(todo)} ok={n} {rate:.2f}/s eta={(len(todo)-i)/rate/60:.1f}m", flush=True)
    c.commit()
    print(f"analyzed {n} in {(time.time()-t0)/60:.1f}m")


def load():
    c = ensure_db()
    paths, vecs = [], []
    for path, dim, blob in c.execute("SELECT path,dim,vec FROM feat"):
        v = np.frombuffer(blob, np.float32)
        if v.shape[0] == dim:
            paths.append(path); vecs.append(v)
    return paths, np.vstack(vecs).astype(np.float64)


def score():
    paths, Z = load()
    idx = {p: i for i, p in enumerate(paths)}
    mu, sd = Z.mean(0), Z.std(0); sd[sd < 1e-9] = 1e-9
    Z = (Z - mu)/sd
    norm = np.linalg.norm(Z, axis=1, keepdims=True); norm[norm < 1e-9] = 1e-9
    Zn = Z/norm
    files = sorted(glob.glob(os.path.join(GT, "*_var_s40_v0.json")))
    # also compare vs deep (100) for spearman
    ov_e, ov_c = [], []
    for f in files:
        d = json.load(open(f, encoding="utf-8")); seed = d["seed"]; si = idx.get(seed)
        if si is None:
            continue
        mip = [t for t in d["tracks"] if t != seed and t in idx][:K]
        if len(mip) < 5:
            continue
        de = np.sqrt(((Z - Z[si])**2).sum(1)); oe = [paths[i] for i in np.argsort(de) if i != si][:K]
        dc = 1 - Zn @ Zn[si]; oc = [paths[i] for i in np.argsort(dc) if i != si][:K]
        ov_e.append(len(set(mip) & set(oe))/K)
        ov_c.append(len(set(mip) & set(oc))/K)
    print(f"\nEXPERIMENTAL (dim={Z.shape[1]}, delta features) vs MusicIP style=40 overlap@{K}:")
    print(f"  euclid: {np.mean(ov_e):.3f}   cosine: {np.mean(ov_c):.3f}   (n={len(ov_e)})")
    print(f"  [baseline 79-dim was ~0.128]")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "score":
        score()
    else:
        analyze()
        score()
