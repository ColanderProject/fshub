Create a python package named fshub. It provide a command `fshub` use flask and HTML5, and support both Windows and Linux (should noticed that the filesystem sepreate mark is different).

`fshub web` will start a web server to provide a UI. This package is used to manage file between different devices. (fshub itself will only working on one device, but we can use some file sync tools to make different device share the meta info and in one fshub client user can view files from different devices)

This project composed by three parts:

1. RESTful web API, the path is startswith `/api/v1/` 
2. static HTML 5 page, use fetch to communicate with the web API. 
3. API document provided by swagger for human to read.

Imagining that a person has many devices: servers, laptops, desktop PCs, mobiles, mobile hard disks, USB disks, etc. . Some time we want to backup files, for example, to retire a server or a laptop, we need backup all files in it. For some files like photo from my friends, or a pdf downloaded from some website, those file may duplicated in different devices. So, we can skip those duplicated files. We can scan all files in each device and calculate the hash of each files then we can know for each file we have how many replicas, if a file is important but the replica count is 1 or all replicas is in mobile device, we can copy it to other device to make it safer. So, we need device manager, backup manager (to know how many file we copied to other place)

`fshub config gen` will create a default yaml config file named `fshub.yaml`, when start web server, will try to load this config file in current path, if not exisit, will try `~/.config/fshub.yaml`. The config includes:

1. data path, default value is '~/.fshub/' 
2. login password by default is random generated string. 
3. If require password. (we need a api to login by password to set cookie, if require password, all API need check cookie, if not valid cookie, ask user to login)
4. listen IP. default localhost
5. Listen port, default 7303

This package does not use relational data base. It store data just in file. All time store in the file use unix timestamp.

This package provides following functions:

1. A page for device manager, device meta data is saved at `device` in data folder. When the web service started, it will load `device/devices_{hostname}.jl` (each line of the jl file is a json obj of a device, actually you need load `device/devices_*.jl` to cover all devices) to load all known devices, and check if current device in this list. If current device are not in it, ask user to add it. For each device, it has following properties: device name(user defined name, default is host name, and you may ask user to modify it before save), device type (PC, Laptop, mobile, etc.), device model, host name, CPU model, memory size, storage space size, IP addr (if has), MAC addr (if has) etc.. and you need create a algorithm to calculate an thumbprint fot the machine.  Save the storage media meta info to `device/media_{hostname}.jl`. Like hard disk, CD-ROM, USB disk etc. The propertise are: device name (gived by user), parent device id(if mobile device not set), size in bytes, crate time.Ins

2. Sanc(path), it can be called by Web API or command. It will use os.walk to visit all files and folders under the target path, example (no need to follow it, you can write the code as you need):
```
def scan(path, counters): # the counter is a dict, used to report scan state (errors count, error msg list, scaned count, scaned size, etc.)
    result = []
    for fpath, dirs, fs in os.walk(path):
        pathObj = {
            'p': '' # cur path
            'f': [], # file list
            'd': [], # dirs list
            't': [], # fileCreatedTimestamp (create time, modify time, access time) in unix timestamp
            'T': [], # dirCreatedTimestamp (create time, modify time, access time) in unix timestamp
            's': [], # file sizes
        }
        # visit all file and dirs and append to result, and also update counters
        result.append(pathObj)
    # save to file
    return result
```
You need use a new thread to run this function. Do not allow running scan on the same path or sub dir or parent dir at the same time (Need a dict to keep running scan task and need a lock to protect the dict). In CLI it will report state every 10 second. In web, user can start scan in web page, and can query state by another API. Provide a UI to view that.
You need add extra info to the first element: device name, device id, CPU model, cpu name, memory size of this machine, host name, ip addr, mac addr, start scan time, finish time.
After scan, save the result to a gzip compressed json line (.jsonl.gz) file in a sub dir (`snapshot`) of the data folder. If user choose use index, will create two file, the first is index file, include `cur path` and `compressed length`, another is .bin file (store compressed fileds except `cur path`), use gzip compress each path object. the file name is snapshot_{timestamp}_{pathCount}
3. The scan result (i.e.) snapshot is stored in disk, and to show it to user, we need load it to memory. it can be called by Web API, load snapshot to a global dict (the key is snapshot file name), and build a index (a dict in memory) from path to the pathObj. Also add a field sub file count and sub dir count and total size of each pathObj during build the index for dispaly it fast. 
After load all dir obj from a snapshot, and build the index, use a recursion function to calculate dir size and total file in all sub dir, store them in the dir obj as 'S' and 'C' (this info should be display in frontend, but not store it in file, calculate it everytime load file)
It support load many snapshot at the same time, in the backend, need a dict to store them, the key is the snapshot file name, the value is the list of the file obj and the index and the group dict. In the frontend, we need a <select> to display the snapshot we have loaded. We also need a API to return all snapshot files, and provide a list for user to select to load.
4. The file explorer page to show the snapshot loaded in memory. at the top of page, show current path, by default is the first node of the snapshot file. '/' is the root path (for windows, '/' include all driver, for example `C:/Users/` is `/C:\\Users\\`). user can click sub path to navigate to other path. To display a path, will call a API to get the result of the path. Display file list (a <table>) and dir list (another <table>) after the path bar. show name, size, create time, modify time (local time "%Y-%m-%d %H:%M:%S"), size (human readable), user can sort by click table header. User can click to hide or display dir table or file table. In this page we use the API to get dir object (`/api/v1/getPath/`), it has six params, 1. he snapshot file name, 2. path to get, 3. index (means the index in the snapshot. The path and the index cannot be used at the same time. For one snapshot, by default, show the first node when load, you can use index 0 to get the first node) 4. A bool value about if use filter or not. 5. Filter in groups (a json list), means in the result, only show the file or dir in those group, if use filter, this API should only return the file or dir belongs to the group user provides, you need check each file and dir in the target path, and to get the dir's size and file in each sub dir, you need vis all sub-dir to get the value according to the group filter. 6. filter out groups (a json list), means in the result should not include the files or dir in those groups.
5. User can add files or dirs to groups for each snapshot. Provide <input> for group name and button to craete group (but when create a new group, it will not be pass to the backend, it only keeps in the frontend as a global var (when page close, it is ok to lost the groups that not have file or dir), only user add some file or dir to the group, the group name will be recorded in the backend). Provide APIs for: 1. Add file to group. 2. Add dir to group. 3. view file and dir in a group. 4. View group list. 5. remove file or dir from group. To make it easy for user to use the feature, in UI, user can select some group as default group, in each file of dir, in the two <table> will have buttom for each default group to add the file or dir to that group. for example <td>File Name</td><td>...</td><td><button>Add to XXX</button> ...</td>
The group result will be store to json line file (.jl). Named as snapshot_{timestamp}_groups.jl. When load a snapshot file, system will try to load the groups file.
The group file will store ['path', 'f' or 'd'(file or dir), groupName, 'add', timestamp]. To remove a group relation, just append a ['path', 'f' or 'd'(file or dir), groupName, 'del', timestamp] to the jl file. when load the file, will create a dict, the key is group name, the value is a set of the path in the group.
6. Search for file or folder by name. can search in all loaded snapshot. In frontend, provide a search box and search button. click search button or click enter in search box, will call search api, when result returned, show search result list. Search also support advance cmd, for example 'ends:xxx' means only show the file or path endswith 'xxx'. 'starts:xxx' means only show the file or path startswith 'xxx'.
7. Filter: filter by group, i.e. not show some groups or only show one or more group. In the web UI, add a filter multi select and a check box to decided if use filter or not. When filter on, the file explorer will not show the filter out items (i.e. user the filter in and filter out parameter in `/api/v1/getPath/`) and only show the filter in (if there are filter in selected). And will recaculated dir size (if not have filter, the dir size has been pre-calculated during loading and building index), but if filter is on, we need calculate dir size according the filter setting when user vis a path.
8. A new page: Show all file sort by size: show a list of the largest file, support paging, default 20 item per page, allow set item per page. support filter, i.e. not show one or more group. For example filter out 'backup' and 'ignore' group, will only show large file not in those two group.
9. Calc file hash manager, ask user use filter (filter in or filter out groups) to select a batch of file to calc hash, when user set filter, first show the file selected and then ask user confirm to start calc hash, put the files in a queue, use thread pool to run hash.
10. A new page backup manager include two backup mode: backup to a zip file (use python zipfile lib to create a zip file, user can select compress level, user can apply a filter to compress the filtered files to the zip file.) and backup to a folder (user also can use filter to decided what file to backup, just copy the file to the target dir) ask user use filter (filter in or filter out groups) to select a batch of file to backup, when user set filter, show file backup info, file list of files in user selected group.

update front end @fshub/templates/index.html. 1. In the <select> of snapshot, add a <option> at the first place, value is "", name is "Select a snapshot to show". 2. When select a snapshot, if the backend not load it. show a button to load snapshot (mean ask the backend read the file to memory and build index), but if the snapshot is loaded by backend, when <select> select it, ask frontend set is as current snapshot, and use the first node path as current path (use /api/v1/getPath to get the index 0, because not all snapshot has `/` path, we need get the index 0 to get the base path). 3. After select a snapshot that is loaded, display the content of index 0. Allow use to click the path to navigator to upper path (for example, current in `/root/document/test`, if user click 'root', go to `/root`. Add a back button to allow user return the privious path (need a stack in front end in memory, it is ok to clear it after page close)

