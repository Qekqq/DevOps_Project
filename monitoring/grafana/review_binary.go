package main

import (
	"crypto/sha256"
	"debug/elf"
	"debug/gosym"
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

func main() {
	if len(os.Args) != 2 {
		panic("usage: review_binary <linux-amd64-plugin>")
	}
	b, e := os.ReadFile(os.Args[1])
	if e != nil {
		panic(e)
	}
	f, e := elf.Open(os.Args[1])
	if e != nil {
		panic(e)
	}
	defer f.Close()
	if f.Machine != elf.EM_X86_64 || f.Section(".gopclntab") == nil || f.Section(".text") == nil {
		panic("expected amd64 Go ELF with runtime function table")
	}
	p, e := f.Section(".gopclntab").Data()
	if e != nil {
		panic(e)
	}
	t, e := gosym.NewTable(nil, gosym.NewLineTable(p, f.Section(".text").Addr))
	if e != nil {
		panic(e)
	}
	matches := []string{}
	controls := []string{}
	for _, fn := range t.Funcs {
		if strings.Contains(fn.Name, "google.golang.org/grpc/xds.") || strings.Contains(fn.Name, "google.golang.org/grpc/internal/xds/server.") {
			matches = append(matches, fn.Name)
		}
		if fn.Name == "google.golang.org/grpc.NewServer" || strings.Contains(fn.Name, "backend.Serve") {
			controls = append(controls, fn.Name)
		}
	}
	if !strings.Contains(strings.Join(controls, "\n"), "google.golang.org/grpc.NewServer") || !strings.Contains(strings.Join(controls, "\n"), "github.com/grafana/grafana-plugin-sdk-go/backend.Serve") {
		panic("positive controls missing; review inconclusive")
	}
	result := map[string]any{"hash": fmt.Sprintf("%x", sha256.Sum256(b)), "functions": len(t.Funcs), "xds_server_symbols": matches, "controls": controls}
	out, _ := json.MarshalIndent(result, "", "  ")
	fmt.Println(string(out))
}
