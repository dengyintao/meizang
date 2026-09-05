#include <iostream>
#include <fstream>
#include <string>
#include <vector>
#include <unordered_map>
#include <boost/filesystem.hpp>
#include <openssl/md5.h>
using namespace std;
namespace fs = boost::filesystem;
// 计算文件的MD5哈希值
string calc_file_md5(const string& file_path)
{
    ifstream ifs(file_path, ios::binary);
    if (!ifs) {
        cerr << "Failed to open file: " << file_path << endl;
        return "";
    }
    MD5_CTX ctx;
    MD5_Init(&ctx);
    char buf[1024];
    while (ifs) {
        ifs.read(buf, sizeof(buf));
        MD5_Update(&ctx, buf, ifs.gcount());
    }
    unsigned char md5[MD5_DIGEST_LENGTH];
    MD5_Final(md5, &ctx);
    string md5_str;
    for (int i = 0; i < MD5_DIGEST_LENGTH; ++i) {
        md5_str += to_string(md5[i]);
    }
    return md5_str;
}
int main(int argc, char* argv[])
{
    if (argc < 2) {
        cerr << "Usage: " << argv[0] << " folder_path" << endl;
        return 1;
    }
    string folder_path = argv[1];
    // 遍历文件夹中的所有图片文件，并计算其MD5哈希值
    unordered_map<string, string> hash_map;
    vector<string> dup_files;
    for (fs::recursive_directory_iterator it(folder_path);
         it != fs::recursive_directory_iterator(); ++it) {
        if (fs::is_regular_file(*it) && it->path().extension() == ".jpg") {
            string file_path = it->path().string();
            string file_md5 = calc_file_md5(file_path);
            if (hash_map.count(file_md5) > 0) {
                // 如果哈希值已经存在，则说明该图片文件与之前的某个图片文件重复
                dup_files.push_back(file_path);
            } else {
                // 否则，将该图片文件的哈希值添加到哈希表中
                hash_map.emplace(file_md5, file_path);
            }
        }
    }
    // 输出重复的图片文件名
    if (!dup_files.empty()) {
        cout << "Duplicate files:" << endl;
        for (const auto& file_path : dup_files) {
            cout << file_path << endl;
        }
    } else {
        cout << "No duplicate files found." << endl;
    }
    return 0;
}
