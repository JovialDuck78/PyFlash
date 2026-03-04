## PyFlash
A USB ISO flashing program written for Linux. I noticed that there was no Rufus alternative for Linux, so I made one!
# What it does:
Functions as a way to write ISOs on external drives more than just DD image mode. You can also now format the drive for the ISO to support windows ISOs. It supports GPT and MBR formatting. You can also use it to reformat external drives in a way similar to GParted just as an added bonus!

| Package | Arch | Debian/Ubuntu | Fedora | Void |
|---|---|---|---|---|
| Python 3 + tkinter | `python tk` | `python3 python3-tk` | `python3 python3-tkinter` | `python3 python3-tkinter` |
| p7zip | `p7zip` | `p7zip-full` | `p7zip p7zip-plugins` | `p7zip` |
| parted | `parted` | `parted` | `parted` | `parted` |
| dosfstools (FAT32) | `dosfstools` | `dosfstools` | `dosfstools` | `dosfstools` |
| exfatprogs (exFAT) | `exfatprogs` | `exfatprogs` | `exfatprogs` | `exfat-utils` |
| ntfs-3g (NTFS) | `ntfs-3g` | `ntfs-3g` | `ntfs-3g` | `ntfs-3g` |

## How to Install Dependencies

**Arch**
```bash
sudo pacman -S python tk p7zip parted dosfstools exfatprogs ntfs-3g
```
**Debian/Ubuntu**
```bash
sudo apt install python3 python3-tk p7zip-full parted dosfstools exfatprogs ntfs-3g
```
**Fedora**
```bash
sudo dnf install python3 python3-tkinter p7zip p7zip-plugins parted dosfstools exfatprogs ntfs-3g
```
**Void**
```bash
sudo xbps-install python3 python3-tkinter p7zip parted dosfstools exfat-utils ntfs-3g
```

## Usage
```bash
sudo python3 pyflash.py
```
