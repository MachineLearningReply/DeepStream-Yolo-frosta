# host1x_fence: fix for the Jetson freeze (L4T 36.4.7)

The Jetson freezes under sustained DeepStream load (CPU 0 stuck in `cuda-EvtHandlr`, see `../FINDINGS.md`).
Cause: a deadlock in NVIDIA's `host1x-fence` kernel driver. In `dev.c`, `host1x_pollfd_release()` and
`host1x_pollfd_poll()` take the fence spinlock with plain `spin_lock()`; if the host1x interrupt arrives on the same
CPU while the lock is held, the interrupt handler waits for the code it interrupted, forever.

NVIDIA identified this in https://forums.developer.nvidia.com/t/rcu-preempt-caused-by-cuda-evthandlr/326076 (page 6).
The fix holds interrupts on that CPU for the instant the lock is held: `spin_lock_irqsave()` / `spin_unlock_irqrestore()`.
The same `dev.c` is unchanged in NVIDIA's 36.4.4, 36.4.7, 36.5 and 36.5.2 sources, so no JetPack 6 release contains the fix yet.

- `host1x-fence-irqsave.patch`: the change (2 lock sites, 1 new variable per function).
- `build_host1x_fence.sh`: builds only this module on the Jetson and checks it. **Installs nothing.**

## 1. Build (on the Jetson)

Source: NVIDIA's `linux-nv-oot` at tag `jetson_36.4.7`, from the official GitLab mirror
`https://gitlab.com/nvidia/nv-tegra/linux-nv-oot` (`nv-tegra.nvidia.com` is often unreachable). Needs the installed
packages `nvidia-l4t-kernel-headers` and `nvidia-l4t-kernel-oot-headers` (36.4.7).

```bash
cd ~/DeepStream-Yolo-frosta && git pull
bash deepstream_pipeline/debug_pipeline/host1x_fence/build_host1x_fence.sh
```

The script stops if anything doesn't match. In order:
- L4T must be 36.4.7, and the kernel must not enforce signed modules.
- It builds the **unchanged** module first. Its vermagic and every symbol checksum must be identical to the installed module.
  This proves the build setup reproduces NVIDIA's module.
- Then it builds the **patched** module. Same vermagic, and every symbol checksum must match the running kernel and NVIDIA drivers
  (the patch adds `_raw_spin_lock_irqsave` / `_raw_spin_unlock_irqrestore`).

Result: `~/host1x-fence-build/host1x-fence.ko.patched`.

## 2. Install (deliberate step, production pipelines stopped)

```bash
M="$(modinfo -n host1x-fence)"                          # /lib/modules/5.15.148-tegra/updates/drivers/gpu/host1x-fence/host1x-fence.ko
[ -f ~/host1x-fence.ko.orig ] || cp "$M" ~/host1x-fence.ko.orig   # backup of NVIDIA's original
sudo cp ~/host1x-fence-build/host1x-fence.ko.patched "$M"
sudo depmod -a
lsinitramfs /boot/initrd | grep -q host1x-fence && sudo nv-update-initrd
sudo reboot
```

## 3. Verify after the reboot

```bash
sha256sum "$(modinfo -n host1x-fence)" ~/host1x-fence-build/host1x-fence.ko.patched   # same hash
lsmod | grep host1x_fence                                                               # loaded
sudo dmesg | grep -i -E "host1x.fence|disagrees|tainted"                               # no load errors
```
Then a quick pipeline start (both cameras, a few minutes) to confirm normal operation, followed by the long test:
the Oct 8 setup (fl1 + fl3, beans, 10 fps, `monitoring/monitor.py`), ≥ 8 h, twice. Before the patch it froze after ~1 h.

## Rollback

```bash
sudo cp ~/host1x-fence.ko.orig "$(modinfo -n host1x-fence)" && sudo depmod -a
lsinitramfs /boot/initrd | grep -q host1x-fence && sudo nv-update-initrd
sudo reboot
```

Note: a JetPack/L4T update (`apt upgrade` of `nvidia-l4t-kernel-oot-modules`) replaces the module with NVIDIA's version
again. Rebuild for the new version, or check whether NVIDIA's release contains the fix.
