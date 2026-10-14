#!/usr/bin/env python3
"""Hermetic tests for proxmox_inventory.py (parsing, one unreadable host) and inventory_check.py (the 0infer miss: a host, a count and a new guest nobody documented).
Run: python3 -m unittest discover -s <dir> -p 'test_inventory.py'"""
import importlib.util, json, os, unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


P, C = load("proxmox_inventory"), load("inventory_check")
OUT = """---VERSION---
pve-manager/9.2.21/abc (running kernel: 7.0.14-23-pve)
---HOST---
pve01
---QEMU---
[{"vmid":701,"name":"infer-pg-primary","status":"running","cpus":32,"maxmem":103079215104},{"vmid":9000,"name":"infer-debian13-template","status":"stopped","cpus":2,"maxmem":2147483648,"template":1}]
---LXC---
[]
---END---
"""


def snap(*hosts):
    return {"hosts": {h: {"hostname": h, "pve_version": "x", "guests": g} for h, g in hosts}}


def guest(vmid, name, template=False, t="qemu"):
    return dict(type=t, vmid=vmid, name=name, status="running", cpus=2, maxmem_mb=2048, template=template)


class InventoryTest(unittest.TestCase):
    def test_parse_reads_version_host_and_both_guest_kinds(self):
        r = P.parse(OUT)
        self.assertEqual((r["hostname"], r["pve_version"].split("/")[0]), ("pve01", "pve-manager"))
        self.assertEqual([(g["vmid"], g["name"], g["maxmem_mb"], g["template"]) for g in r["guests"]], [(701, "infer-pg-primary", 98304, False), (9000, "infer-debian13-template", 2048, True)])

    def test_one_unreadable_host_does_not_hide_the_others(self):
        def run(alias, timeout):
            if alias == "down":
                raise RuntimeError("ssh down failed")
            return OUT
        s, bad = P.take([{"id": "a", "alias": "up"}, {"id": "b", "alias": "down"}], run=run)
        self.assertEqual(bad, 1)
        self.assertIn("guests", s["hosts"]["a"])
        self.assertIn("error", s["hosts"]["b"])

    def test_not_a_proxmox_host_is_an_error(self):
        with self.assertRaises(ValueError):
            P.parse("---VERSION---\n---HOST---\n")

    def test_host_missing_from_the_docs_and_a_wrong_count_are_found(self):
        s = snap(("0docker", [guest(100, "web")]), ("0infer", [guest(701, "infer-pg-primary")]))
        res = C.check(s, ["The fleet spans four Proxmox hosts. 0docker runs VM 100."])
        kinds = sorted(r["kind"] for r in res)
        self.assertEqual(kinds, ["host_count_mismatch", "host_undocumented"])
        self.assertTrue(any("0infer" in r["detail"] for r in res if r["kind"] == "host_undocumented"))

    def test_clean_when_docs_match(self):
        s = snap(("0docker", [guest(100, "web")]), ("0infer", []))
        self.assertEqual(C.check(s, ["Five hosts? no: two Proxmox hosts: 0docker and 0infer."]), [])

    def test_only_new_guests_must_be_documented(self):
        prev = snap(("0docker", [guest(100, "web")]))
        now = snap(("0docker", [guest(100, "web"), guest(101, "newvm"), guest(102, "tmpl", template=True), guest(103, "documented"), guest(104, "viaid")]))
        docs = ["0docker: the `documented` VM, and VM 104 too"]
        res = C.check(now, docs, prev)
        self.assertEqual([r["guest"] for r in res], ["qemu 101 newvm"])

    def test_a_new_host_makes_all_its_guests_new_and_the_allowlist_silences(self):
        prev = snap(("0docker", []))
        now = snap(("0docker", []), ("0infer", [guest(701, "infer-pg-primary")]))
        res = C.check(now, ["0docker and 0infer"], prev)
        self.assertEqual([r["guest"] for r in res], ["qemu 701 infer-pg-primary"])
        self.assertEqual(C.check(now, ["0docker and 0infer"], prev, {"guests": ["701"]}), [])

    def test_an_unreadable_host_is_reported_and_blocks_the_count_check(self):
        s = {"hosts": {"a": {"hostname": "a", "pve_version": "x", "guests": []}, "b": {"error": "ssh b failed"}}}
        res = C.check(s, ["three Proxmox hosts a b c"])
        self.assertEqual([r["kind"] for r in res], ["host_unreadable"])


if __name__ == "__main__":
    unittest.main()


class WeeklyTest(unittest.TestCase):
    """bin/inventory_weekly.py against a local bare remote called origin and a clone called fleet-state (the generator needs that directory name)."""

    def setUp(self):
        import subprocess, tempfile
        self.W = load("inventory_weekly")
        self.W.notify = lambda text: None
        self.tmp = tempfile.TemporaryDirectory()
        r = self.tmp.name
        self.remote = os.path.join(r, "origin.git")
        self.fs = os.path.join(r, "fleet-state")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        run = lambda *a: subprocess.run(list(a), check=True, env=env, capture_output=True, text=True)
        run("git", "init", "-q", "--bare", "-b", "master", self.remote)
        run("git", "clone", "-q", self.remote, self.fs)
        os.makedirs(os.path.join(self.fs, "inventory"))
        with open(os.path.join(self.fs, "inventory", "proxmox-hosts.json"), "w") as f:
            json.dump([{"id": "0infer", "alias": "x"}], f)
        with open(os.path.join(self.fs, "OPS.md"), "w") as f:
            f.write("The fleet has one Proxmox hosts: 0infer.\n<!-- BEGIN GENERATED:proxmox-inventory host=0infer -->\nhand typed\n<!-- END GENERATED:proxmox-inventory -->\n")
        run("git", "-C", self.fs, "checkout", "-q", "-b", "master")
        run("git", "-C", self.fs, "add", "-A"); run("git", "-C", self.fs, "commit", "-q", "-m", "init"); run("git", "-C", self.fs, "push", "-q", "origin", "master")
        self.guests = [guest(701, "infer-pg-primary")]

    def tearDown(self):
        self.tmp.cleanup()

    def take(self, hosts):
        return {"taken_at": "now", "hosts": {"0infer": {"hostname": "pve01", "pve_version": "x", "guests": list(self.guests)}}}, 0

    def go(self):
        return self.W.run(self.fs, take=self.take, issue=False)

    def remote_file(self, path):
        import subprocess
        return subprocess.run(["git", "-C", self.remote, "show", "master:" + path], capture_output=True, text=True).stdout

    def test_first_run_writes_snapshot_baseline_and_generated_table_and_pushes(self):
        rc, rep = self.go()
        self.assertEqual(rc, 0)
        self.assertEqual(rep["commits"], ["snapshot", "tables", "baseline"])
        self.assertIn("infer-pg-primary", self.remote_file("OPS.md"))
        self.assertNotIn("hand typed", self.remote_file("OPS.md"))
        self.assertIn("infer-pg-primary", self.remote_file("inventory/proxmox-baseline.json"))

    def test_a_new_undocumented_guest_is_a_finding_and_keeps_the_baseline(self):
        self.go()
        self.guests.append(guest(704, "infer-tier0"))
        rc, rep = self.go()
        # the generated table now lists it, so the docs mention it: documented by generation
        self.assertEqual(rc, 0)
        self.assertIn("infer-tier0", self.remote_file("OPS.md"))
        self.assertIn("infer-tier0", self.remote_file("inventory/proxmox-baseline.json"))

    def test_a_new_guest_on_a_host_without_a_generated_block_is_a_finding(self):
        import subprocess
        self.go()
        with open(os.path.join(self.fs, "inventory", "proxmox-hosts.json"), "w") as f:
            json.dump([{"id": "0infer", "alias": "x"}, {"id": "0docker", "alias": "y"}], f)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        for a in (["add", "-A"], ["commit", "-q", "-m", "more hosts"], ["push", "-q", "origin", "master"]):
            subprocess.run(["git", "-C", self.fs] + a, check=True, env=env, capture_output=True)
        def take(hosts):
            return {"taken_at": "t2", "hosts": {"0infer": {"hostname": "p", "pve_version": "x", "guests": list(self.guests)}, "0docker": {"hostname": "d", "pve_version": "x", "guests": [guest(100, "mystery-vm")]}}}, 0
        rc, rep = self.W.run(self.fs, take=take, issue=False)
        kinds = sorted(f["kind"] for f in rep["findings"])
        self.assertEqual(rc, 1)
        self.assertIn("host_undocumented", kinds)
        self.assertIn("host_count_mismatch", kinds)
        self.assertNotIn("0docker", self.remote_file("inventory/proxmox-baseline.json"))  # the baseline did not advance

    def test_an_unreadable_host_changes_nothing(self):
        def take(hosts):
            return {"taken_at": "t", "hosts": {"0infer": {"error": "ssh x failed"}}}, 1
        rc, rep = self.W.run(self.fs, take=take, issue=False)
        self.assertEqual(rc, 1)
        self.assertFalse(rep["committed"])
        self.assertEqual(self.remote_file("inventory/proxmox-snapshot.json"), "")
