# 20240218
import os
import shutil
#import smbclient
import pathlib


def smb_to_local(remote_file: str) -> str:
    file_name = pathlib.Path(remote_file).name
    if not os.path.exists('./tmp'):
        os.mkdir('./tmp')
    local_file = pathlib.Path('./tmp').joinpath(file_name)
    if os.name == 'nt':
        shutil.copyfile(remote_file, str(local_file.absolute()))
    else:
        print(f"[#error#] now in smb_to_local, {os.name}")
        exit(1)
    return str(local_file.absolute())


def local_to_smb(file: str, smb_path) -> None:
    if os.name == 'nt':
        shutil.copyfile(file, smb_path)
    else:
        print(f"[#error#] now in local_to_smb, {os.name}")
        exit(1)


if __name__ == '__main__':
    local = smb_to_local('\\\\Gen8\\d\\Nippon\\JAV_output\\时田亜美\\FSDSS-516\\thumb.jpg')
    print(local)

    local_to_smb('E:\\Code\\Movie_Data_Capture\\utils\\tmp\\thumb.jpg',
                 '\\\\Gen8\\d\\Nippon\\JAV_output\\时田亜美\\FSDSS-516\\thumb.jpg')
