"""Keep Unicode messages out of cmd.exe's batch parser."""
import sys

if len(sys.argv) > 1 and sys.argv[1] != '0':
    print('操作未完成，请查看上方错误信息。', flush=True)
print('请按任意键关闭此窗口。', flush=True)
