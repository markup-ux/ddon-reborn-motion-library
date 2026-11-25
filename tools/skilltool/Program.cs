using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Arrowgene.Ddon.Client;

// Usage:
//   skilltool scan <romDir> <ext1,ext2,...>   -> find arcs containing files of given extensions
//   skilltool dump <arcFile> <ext>            -> extract all files of ext from one arc into ./dump

if (args.Length < 1)
{
    Console.WriteLine("commands: scan <romDir> <exts>  |  dump <arc> <ext>");
    return 1;
}

if (args[0] == "scan")
{
    string romDir = args[1];
    string[] exts = args[2].Split(',', StringSplitOptions.RemoveEmptyEntries);
    var arcs = Directory.EnumerateFiles(romDir, "*.arc", SearchOption.AllDirectories);
    int scanned = 0;
    foreach (string arc in arcs)
    {
        scanned++;
        if (scanned % 2000 == 0) Console.Error.WriteLine($"  ...scanned {scanned}");
        try
        {
            using FileStream fs = File.OpenRead(arc);
            foreach (string ext in exts)
            {
                fs.Position = 0;
                var search = ArcArchive.Search().ByExtension(ext);
                var indices = ArcArchive.IndexProbeStream(fs, search);
                if (indices.Count > 0)
                {
                    Console.WriteLine($"{ext}\t{indices.Count}\t{arc}");
                    foreach (var idx in indices.Take(5))
                        Console.WriteLine($"      -> {idx.Path}");
                }
            }
        }
        catch
        {
            // ignore non-arc / unreadable
        }
    }
    Console.Error.WriteLine($"done. scanned {scanned} arcs.");
    return 0;
}

if (args[0] == "dump")
{
    string arc = args[1];
    string ext = args[2];
    var archive = new ArcArchive();
    archive.Open(arc);
    var search = ArcArchive.Search().ByExtension(ext);
    var files = archive.GetFiles(search);
    Directory.CreateDirectory("dump");
    Console.WriteLine($"{files.Count} files with ext {ext}");
    foreach (var f in files)
    {
        string outPath = Path.Combine("dump", f.Index.Path.Replace('\\', '_').Replace('/', '_'));
        File.WriteAllBytes(outPath, f.Data);
        Console.WriteLine($"  {f.Index.Path}  size={f.Data.Length}  -> {outPath}");
    }
    return 0;
}

return 1;
