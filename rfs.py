"""rfs - Remote File Server CLI client."""

import os
import re

import click
import requests

DEFAULT_SERVER = "http://10.177.59.150:8580"
DEFAULT_PROXY = "http://172.25.104.139:7890"


@click.group()
@click.option("--server", default=DEFAULT_SERVER, help="Server base URL.")
@click.option("--proxy", default=DEFAULT_PROXY, help="HTTP proxy address.")
@click.option("--no-proxy", is_flag=True, help="Disable proxy.")
@click.pass_context
def cli(ctx, server, proxy, no_proxy):
    """Remote File Server CLI client."""
    ctx.ensure_object(dict)
    ctx.obj["server"] = server.rstrip("/")
    ctx.obj["proxies"] = None if no_proxy else {"http": proxy, "https": proxy}


@cli.command()
@click.argument("path", default="")
@click.pass_context
def ls(ctx, path):
    """List files on the remote server."""
    server = ctx.obj["server"]
    proxies = ctx.obj["proxies"]

    url = f"{server}/{path}"
    if path and not url.endswith("/"):
        url += "/"

    resp = requests.get(url, proxies=proxies, timeout=30)
    resp.raise_for_status()

    # Parse <a> tags from the HTML directory listing
    links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>([^<]*)</a>', resp.text)

    if not links:
        click.echo("(empty)")
        return

    for href, name in links:
        # Skip parent directory link
        if href == "../" or name.strip() == "..":
            continue
        display_name = name.strip() or href
        entry_type = "DIR " if href.endswith("/") else "FILE"
        click.echo(f"  {entry_type}  {display_name}")


@cli.command()
@click.argument("local_file", type=click.Path(exists=True))
@click.option("--dest", default="/", help="Remote destination directory.")
@click.pass_context
def upload(ctx, local_file, dest):
    """Upload a local file to the remote server."""
    server = ctx.obj["server"]
    proxies = ctx.obj["proxies"]

    dest = dest.rstrip("/") + "/"
    url = f"{server}{dest}"

    filename = os.path.basename(local_file)
    with open(local_file, "rb") as f:
        files = {"file": (filename, f)}
        resp = requests.post(url, files=files, proxies=proxies, timeout=120)

    resp.raise_for_status()
    click.echo(f"Uploaded: {filename} -> {dest}{filename}")


@cli.command()
@click.argument("remote_path")
@click.option("-o", "--output", default=None, help="Local output path.")
@click.pass_context
def download(ctx, remote_path, output):
    """Download a file from the remote server."""
    server = ctx.obj["server"]
    proxies = ctx.obj["proxies"]

    url = f"{server}/{remote_path.lstrip('/')}"

    if output is None:
        output = os.path.basename(remote_path)

    resp = requests.get(url, proxies=proxies, timeout=120, stream=True)
    resp.raise_for_status()

    with open(output, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    click.echo(f"Downloaded: {remote_path} -> {output}")


if __name__ == "__main__":
    cli()
