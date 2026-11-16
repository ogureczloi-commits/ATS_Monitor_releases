import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime


def run_hidden(cmd):
    kwargs = {
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
    }
    if hasattr(subprocess, 'CREATE_NO_WINDOW'):
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.call(cmd, **kwargs)


def wait_for_pid(pid, timeout=30):
    """Wait for the exact process launched by the app, not every same-named EXE."""
    if not pid:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            if str(pid) not in result.stdout:
                return True
        except Exception:
            return True
        time.sleep(0.25)
    return False


def stage_updater(source_path, app_name):
    """Run the updater outside the install directory so its file can be replaced."""
    stage_dir = os.path.join(tempfile.gettempdir(), f'{app_name.replace(" ", "_")}_updater')
    os.makedirs(stage_dir, exist_ok=True)
    staged_path = os.path.join(stage_dir, f'updater_{os.getpid()}_{int(time.time() * 1000)}.exe')
    shutil.copy2(source_path, staged_path)
    return staged_path


def is_process_running(exe_name):
    exe_name = exe_name.lower()
    try:
        out = subprocess.check_output(
            ['tasklist', '/FI', f'IMAGENAME eq {exe_name}'],
            text=True,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        ).lower()
        return exe_name in out
    except Exception:
        return False


def wait_for_process_exit(exe_name, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_process_running(exe_name):
            return True
        time.sleep(0.5)
    return False


def unlock_old_app(exe_name):
    # Если основное окно закрылось, но tray/потоки ещё держат exe, принудительно добиваем.
    # ВАЖНО: НЕ используем /T. updater.exe запущен дочерним процессом от ats_monitor_ui.exe,
    # и taskkill /T убивает вместе со старой программой сам обновлятор.
    if is_process_running(exe_name):
        run_hidden(['taskkill', '/F', '/IM', exe_name])
        wait_for_process_exit(exe_name, timeout=10)


def find_payload_dir(extract_dir, exe_name):
    # Архивы бывают двух видов:
    # 1) внутри сразу ats_monitor_ui.exe + _internal
    # 2) внутри папка ats_monitor_ui/ats_monitor_ui.exe + _internal
    direct = os.path.join(extract_dir, exe_name)
    if os.path.exists(direct):
        return extract_dir
    for root, dirs, files in os.walk(extract_dir):
        if exe_name in files and '_internal' in dirs:
            return root
    for root, dirs, files in os.walk(extract_dir):
        if exe_name in files:
            return root
    raise RuntimeError(f'В архиве не найден {exe_name}')


def safe_remove(path):
    if not os.path.exists(path):
        return
    try:
        os.chmod(path, 0o777)
    except Exception:
        pass
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


LOCAL_STATE_FILES = {
    'openweather_api_key.txt',
    'esp_last_ip.txt',
    'ats_monitor_local_config.json',
    'fan_endpoint.txt',
    'update_error.txt',
}


def copy_tree_contents(src, dst):
    for name in os.listdir(src):
        # Локальная конфигурация и секреты не поставляются с GitHub Releases.
        if name.lower() in LOCAL_STATE_FILES:
            continue
        src_path = os.path.join(src, name)
        dst_path = os.path.join(dst, name)
        safe_remove(dst_path)
        if os.path.isdir(src_path):
            shutil.copytree(src_path, dst_path)
        else:
            shutil.copy2(src_path, dst_path)


def main():
    parser = argparse.ArgumentParser(description='ATS Monitor updater')
    parser.add_argument('--app-dir', required=True)
    parser.add_argument('--zip', required=True)
    parser.add_argument('--exe', default='ats_monitor_ui.exe')
    parser.add_argument('--wait-pid', type=int, default=0)
    args = parser.parse_args()

    app_dir = os.path.abspath(args.app_dir)
    zip_path = os.path.abspath(args.zip)
    exe_name = args.exe
    parent = os.path.dirname(app_dir)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = os.path.join(parent, f'_backup_ats_monitor_{stamp}')
    extract_dir = os.path.join(parent, f'_update_extract_ats_monitor_{stamp}')

    # Wait for the exact old GUI process. The updater itself runs from Temp,
    # so updater.exe in the application directory is safe to replace.
    if not wait_for_pid(args.wait_pid, timeout=30):
        raise_timeout = RuntimeError(f'Программа не завершилась за 30 секунд (PID {args.wait_pid})')
        err_path = os.path.join(app_dir, 'update_error.txt')
        with open(err_path, 'w', encoding='utf-8') as f:
            f.write(str(raise_timeout))
        return 1

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            bad_file = zf.testzip()
            if bad_file:
                raise RuntimeError(f'Повреждён файл в архиве: {bad_file}')
            zf.extractall(extract_dir)
        payload_dir = find_payload_dir(extract_dir, exe_name)

        if os.path.exists(backup_dir):
            shutil.rmtree(backup_dir, ignore_errors=True)
        shutil.copytree(app_dir, backup_dir, ignore=shutil.ignore_patterns('update_error.txt'))

        copy_tree_contents(payload_dir, app_dir)

        err_path = os.path.join(app_dir, 'update_error.txt')
        if os.path.exists(err_path):
            safe_remove(err_path)

        new_exe = os.path.join(app_dir, exe_name)
        subprocess.Popen([new_exe], cwd=app_dir, close_fds=True)
    except Exception as e:
        # Откат, если что-то пошло не так.
        try:
            if os.path.exists(backup_dir):
                copy_tree_contents(backup_dir, app_dir)
                subprocess.Popen([os.path.join(app_dir, exe_name)], cwd=app_dir, close_fds=True)
        except Exception:
            pass
        err_path = os.path.join(app_dir, 'update_error.txt')
        with open(err_path, 'w', encoding='utf-8') as f:
            f.write(str(e))
        return 1
    finally:
        try:
            if os.path.exists(extract_dir):
                shutil.rmtree(extract_dir, ignore_errors=True)
        except Exception:
            pass

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
