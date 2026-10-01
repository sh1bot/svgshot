/* Architecture-specific launcher for the adjacent portable capture archive.
 * Runtime: Python 3.10+, PyGObject/AT-SPI2 and Pillow from the Linux distribution.
 */
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <limits.h>
int main(int argc, char **argv) {
    char exe[PATH_MAX], archive[PATH_MAX];
    ssize_t n=readlink("/proc/self/exe",exe,sizeof(exe)-1);
    if(n<0 || n>=(ssize_t)sizeof(exe)-1) {perror("svgshot capture executable path");return 1;}
    exe[n]=0; char *slash=strrchr(exe,'/'); if(slash)*slash=0;
    if(snprintf(archive,sizeof(archive),"%s/svgshot-grab.pyz",exe)>=(int)sizeof(archive))return 1;
    char **args=calloc((size_t)argc+2,sizeof(char*));if(!args)return 1;
    const char *python=getenv("SVGSHOT_PYTHON");if(!python)python="/usr/bin/python3";
    args[0]=(char*)python;args[1]=archive;for(int i=1;i<argc;i++)args[i+1]=argv[i];
    execv(python,args);perror("svgshot capture Python runtime");free(args);return 1;
}
