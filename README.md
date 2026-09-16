<<<<<<< HEAD
# ResearchAssistant

ResearchAssistant là ứng dụng desktop đọc và quản lý bài báo nghiên cứu. Ứng
dụng chạy cục bộ bằng Python, PySide6 và PDF.js; các khóa API được lưu qua
keyring của hệ điều hành.

## Tải bản cài đặt

Nếu chỉ muốn sử dụng ứng dụng, tải bản phát hành mới nhất tại:

<https://drive.google.com/drive/folders/1cHuSBctUC_5pvx83iiUYsHzexDJQUYKG?hl=vi>


## Yêu cầu

- Python 3.11 trở lên.
- Git.
- Khoảng 2 GB dung lượng trống cho môi trường Python và Qt WebEngine.

Trên Ubuntu/Debian, cài các thư viện hệ thống:

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv libasound2 libdbus-1-3 libegl1 libfontconfig1 libgl1 \
  libglib2.0-0 libnss3 libx11-6 libx11-xcb1 libxcb1 libxcb-cursor0 \
  libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
  libxkbcommon-x11-0 libxrandr2 libxrender1 libxtst6
```

## Cài môi trường và chạy bằng Python

Linux/macOS:

```bash
git clone https://github.com/akihito261/ResearchAssistant.git
cd ResearchAssistant
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

Windows PowerShell:

```powershell
git clone https://github.com/akihito261/ResearchAssistant.git
cd ResearchAssistant
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

Nếu PowerShell chặn script kích hoạt, chạy một lần trong cửa sổ hiện tại:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

## Dữ liệu cục bộ

Khi chạy trực tiếp từ source, ứng dụng sử dụng các thư mục `data/`, `library/`,
`logs/`, `backups/` và `config/`. Các thư mục này đã được `.gitignore` loại trừ
để database, PDF, log, bản sao lưu và cấu hình cá nhân không bị đưa lên GitHub.

Không commit API key hoặc credential. Ứng dụng sử dụng keyring của hệ điều hành
cho thông tin xác thực.

## Kiểm tra trước khi đóng góp

```bash
python -m compileall -q main.py app
python -m unittest discover -s tests
```

Giữ thay đổi nhỏ, không commit dữ liệu cá nhân, môi trường ảo hoặc artifact đã
build. PDF.js và các tài nguyên runtime trong `resources/` là thành phần cần
thiết để chạy ứng dụng và phải được giữ lại.
=======
# ResearchAssistant
>>>>>>> 59d2694b649569675a1ddaf88cff0860a854cd52
