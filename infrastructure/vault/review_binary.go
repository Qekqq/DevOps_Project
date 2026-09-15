// Read-only evidence collector. Run with Go against the official Linux binary.
// Symbol absence alone is insufficient: also inspect DWARF subprogram names,
// which include abstract origins used by inlined code. This does not alter gates.
package main

import (
	"crypto/sha256"
	"debug/dwarf"
	"debug/elf"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
)

func main() {
	if len(os.Args) != 2 { panic("expected binary path") }
	f, err := os.Open(os.Args[1]); if err != nil { panic(err) }
	defer f.Close()
	h := sha256.New(); if _, err = io.Copy(h, f); err != nil { panic(err) }
	e, err := elf.NewFile(f); if err != nil { panic(err) }
	syms, err := e.Symbols(); if err != nil { panic(err) }
	d, err := e.DWARF(); if err != nil { panic(err) }
	names := make(map[string]bool)
	for _, s := range syms { if elf.ST_TYPE(s.Info) == elf.STT_FUNC { names[s.Name] = true } }
	r := d.Reader(); programs := 0
	for {
		entry, err := r.Next(); if err != nil { panic(err) }; if entry == nil { break }
		if entry.Tag == dwarf.TagSubprogram {
			programs++
			if name, ok := entry.Val(dwarf.AttrName).(string); ok { names[name] = true }
		}
	}
	patterns := []string{
		"google.golang.org/grpc/xds.NewGRPCServer",
		"google.golang.org/grpc/xds.xdsUnaryInterceptor",
		"google.golang.org/grpc/xds.xdsStreamInterceptor",
		"google.golang.org/grpc/internal/xds/server.RouteAndProcess",
		"github.com/apache/thrift/lib/go/thrift.(*TCompactProtocol)",
		"github.com/apache/thrift/lib/go/thrift.NewTCompactProtocol",
	}
	counts := make(map[string]int)
	for _, pattern := range patterns {
		counts[pattern] = 0
		for name := range names { if strings.Contains(name, pattern) { counts[pattern]++ } }
	}
	controls := []string{"github.com/apache/thrift/lib/go/thrift.Skip", "google.golang.org/grpc.NewServer"}
	for _, name := range controls { if !names[name] { panic("missing positive control: " + name) } }
	if programs == 0 { panic("DWARF subprogram information absent") }
	result := map[string]any{"binary_sha256": hex.EncodeToString(h.Sum(nil)), "elf_machine": e.Machine.String(), "dwarf_subprograms": programs, "function_names": len(names), "positive_controls": controls, "matches": counts}
	data, err := json.MarshalIndent(result, "", "  "); if err != nil { panic(err) }
	fmt.Println(string(data))
}
