"""Compute the minimal set of track paths needed to validate against ground truth:
every seed plus the union of its deep-ranking neighbours (top-N). Writing this list
lets scan.py analyze just the validation-relevant tracks first, so we get overlap
numbers without waiting for the whole 17k library.
"""
import os, json, glob, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
GT = os.environ.get("ATTUNE_GT", os.path.join(HERE, "..", "data", "groundtruth"))


def main(topn, out, max_seeds):
    files = sorted(glob.glob(os.path.join(GT, "*_deep_s40_v0.json")))
    if max_seeds:
        files = files[:max_seeds]
    want = set()
    for fp in files:
        d = json.load(open(fp, encoding="utf-8"))
        want.add(d["seed"])
        for t in d["tracks"][:topn]:
            want.add(t)
    want = sorted(want)
    with open(out, "w", encoding="utf-8") as f:
        for p in want:
            f.write(p + "\n")
    print(f"{len(files)} seeds, top{topn} neighbours -> {len(want)} distinct tracks")
    print(f"wrote {os.path.abspath(out)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--topn", type=int, default=40)
    ap.add_argument("--max-seeds", type=int, default=0, help="0 = all seeds")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "data", "validation_paths.txt"))
    a = ap.parse_args()
    main(a.topn, a.out, a.max_seeds)
