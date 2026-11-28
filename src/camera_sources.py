"""Linux camera source discovery and user-visible source labels."""
import errno
import hashlib
import os
from pathlib import Path
import re
import struct

if __package__:
    from . import wide_angle
else:
    import wide_angle

SOURCE_LABELS = {"realsense": "RealSense", "v4l2": "V4L2 RGB", "none": "No camera",
                 "rtsp": wide_angle.LABEL}


def v4l2_formats(device):
    """Read Linux V4L2 capabilities/formats without configuring or starting capture."""
    import fcntl  # Keep the module importable on Windows and macOS.

    descriptor = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    try:
        # Linux videodev2.h: v4l2_capability (104 bytes), v4l2_fmtdesc (64 bytes).
        capabilities = bytearray(104)
        fcntl.ioctl(descriptor, 0x80685600, capabilities)  # VIDIOC_QUERYCAP
        caps, device_caps = struct.unpack_from("=II", capabilities, 84)
        if caps & 0x80000000:  # V4L2_CAP_DEVICE_CAPS: use this node, not the parent union.
            caps = device_caps
        if not caps & 0x00000001 or not caps & 0x04000000:  # VIDEO_CAPTURE and STREAMING
            return set()
        formats = set()
        for index in range(256):
            description = bytearray(64)
            struct.pack_into("=II", description, 0, index, 1)  # VIDEO_CAPTURE buffer type
            try:
                fcntl.ioctl(descriptor, 0xc0405602, description)  # VIDIOC_ENUM_FMT
            except OSError as error:
                if error.errno == errno.EINVAL:
                    break
                raise
            fourcc = struct.unpack_from("=I", description, 44)[0]
            formats.add(struct.pack("<I", fourcc).decode("ascii", errors="replace"))
        if formats and not os.access(device, os.R_OK | os.W_OK):
            raise PermissionError(errno.EACCES, "Capture requires read/write access", device)
        return formats
    finally:
        os.close(descriptor)


def camera_device_owners(proc_root):
    """Best-effort read-only scan; never treat our shared capture as a conflict."""
    owners = {}
    try:
        processes = list(Path(proc_root).iterdir())
    except OSError:
        return owners
    for process in processes:
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except OSError:
            continue  # Process exited or procfs denies access to its descriptors.
        for descriptor in descriptors:
            try:
                device = os.readlink(str(descriptor))
            except OSError:
                continue
            if not re.fullmatch(r"/dev/(video[0-9]+|bus/usb/[0-9]+/[0-9]+)", device):
                continue
            try:
                name = Path(os.readlink(str(process / "exe"))).name
            except OSError:
                try:
                    name = (process / "comm").read_text(errors="replace").strip()
                except OSError:
                    name = "another process"
            owners[device] = "{} (PID {})".format(name, process.name)
    return owners


def sysfs_rgb_node(node, usb, kind):
    """Identify an inaccessible RGB node without guessing its /dev/video number."""
    if (node / "index").read_text().strip() != "0":
        return False  # UVC metadata has a separate index on the same interface.
    if kind == "sxw":
        return True
    names = [(node / "name").read_text()]
    for parent in node.resolve().parents:
        if parent == usb:
            break
        if (parent / "interface").is_file():
            names.append((parent / "interface").read_text())
    return any(re.search(r"\brgb\b", name, re.I) for name in names)


def discover_sources(sysfs_root=Path("/sys/class/video4linux"), probe=None, proc_root=Path("/proc")):
    """Identify supported RGB nodes through their USB parent and capture formats."""
    probe = probe or v4l2_formats
    root = Path(sysfs_root)
    try:
        nodes = [node for node in root.iterdir() if re.fullmatch(r"video[0-9]+", node.name)]
    except FileNotFoundError:
        return []
    owners = camera_device_owners(proc_root)
    found = []
    for node in sorted(nodes, key=lambda item: int(item.name[5:])):
        try:
            resolved = node.resolve()
            usb = next((parent for parent in resolved.parents if (parent / "idVendor").is_file()), None)
            if usb is None:
                continue
            vendor = (usb / "idVendor").read_text().strip().lower()
            product_id = (usb / "idProduct").read_text().strip().lower()
            product = (usb / "product").read_text().strip() if (usb / "product").is_file() else ""
            serial = (usb / "serial").read_text().strip() if (usb / "serial").is_file() else ""
            if (vendor, product_id) == ("8086", "0b3a") or (
                    "realsense" in product.lower() and re.search(r"435i\b", product, re.I)):
                kind, label, pixel_format = "d435i", "D435i RGB", "YUYV"
            elif (vendor, product_id) == ("0bda", "525a"):
                kind, label, pixel_format = "sxw", "Arm camera (SXW USB Camera)", "MJPG"
            else:
                continue
            device = "/dev/" + node.name
            error = None
            try:
                if pixel_format not in probe(device):
                    continue
            except (OSError, ImportError) as failure:
                if not sysfs_rgb_node(node, usb, kind):
                    continue
                error = "Cannot inspect {}: {}. Check camera usage, connection and permissions; then Refresh.".format(device, failure)
            # SDK capture can own a sibling depth interface or the USB device itself.
            related = ["/dev/" + sibling.name for sibling in nodes if usb in sibling.resolve().parents]
            try:
                related.append("/dev/bus/usb/{:03d}/{:03d}".format(
                    int((usb / "busnum").read_text()), int((usb / "devnum").read_text())))
            except (OSError, ValueError):
                pass
            busy = next((path for path in related if path in owners), None)
            if busy:
                error = "Camera is in use by {} ({}). Release it in that application, then Refresh.".format(owners[busy], busy)
            identity = "\0".join((str(resolved), vendor, product_id, serial))
            source_id = "camera-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            found.append({"id": source_id, "label": device + " — " + label, "device": device,
                          "backend": "v4l2", "kind": kind, "serial": serial,
                          "usb_path": str(usb), "pixel_format": pixel_format,
                          "available": error is None, "error": error})
        except (OSError, ImportError):
            # An inaccessible/disconnected node must not hide other supported cameras.
            continue
    return found
